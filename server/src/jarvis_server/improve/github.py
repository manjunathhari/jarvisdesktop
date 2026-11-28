"""The team's GitHub App: an installation token for this one repo, open or update its own PR, and
read a PR's state. There is no merge and no approve here, by design: only Manj merges.

The App's private key is the improver's secret `improve_github_app_key`; the token it mints lasts
an hour and asks only for contents and pull requests (write) and checks (read) on this repo. The
branch ruleset on GitHub limits the App to improve/** branches, so even this token can't touch main.
Standard library only (urllib honours HTTPS_PROXY); `send` is injectable for tests.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from jarvis_server.improve.review_loop import Comment, Pr

API = "https://api.github.com"
Send = Callable[[str, str, dict[str, str], Any], Any]

_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
_FINE = frozenset({"success", "neutral", "skipped"})


class GitHubError(Exception):
    pass


def _urllib_send(method: str, url: str, headers: dict[str, str], body: Any) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https only
            text = response.read().decode()
    except urllib.error.HTTPError as e:
        raise GitHubError(f"{method} {url.removeprefix(API)}: HTTP {e.code}") from None
    return json.loads(text) if text else None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GitHubApp:
    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        repo: str,
        send: Send = _urllib_send,
        clock: Callable[[], float] = time.time,
    ) -> None:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise GitHubError("the GitHub App key must be an RSA private key")
        self._key = key
        self.app_id, self.installation_id = app_id, installation_id
        self.owner, self.name = repo.split("/", 1)
        self._send, self._clock = send, clock
        self._token: tuple[str, float] | None = None

    def _jwt(self) -> str:
        now = int(self._clock())
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(
            json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode()
        )
        sig = self._key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{claims}.{_b64url(sig)}"

    def token(self) -> str:
        if self._token and self._token[1] - 300 > self._clock():
            return self._token[0]
        answer = self._send(
            "POST",
            f"{API}/app/installations/{self.installation_id}/access_tokens",
            {**_HEADERS, "Authorization": f"Bearer {self._jwt()}"},
            {
                "repositories": [self.name],
                "permissions": {"contents": "write", "pull_requests": "write", "checks": "read"},
            },
        )
        expires = datetime.fromisoformat(answer["expires_at"].replace("Z", "+00:00")).timestamp()
        self._token = (answer["token"], expires)
        return self._token[0]

    def git_auth_header(self) -> str:
        pair = base64.b64encode(f"x-access-token:{self.token()}".encode()).decode()
        return f"AUTHORIZATION: basic {pair}"

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {**_HEADERS, "Authorization": f"Bearer {self.token()}"}
        return self._send(method, f"{API}/repos/{self.owner}/{self.name}{path}", headers, body)

    def open_or_update_pr(self, branch: str, title: str, body: str) -> tuple[int, str]:
        if not branch.startswith("improve/"):
            raise GitHubError(f"the team opens PRs only from improve/ branches, not {branch!r}")
        query = urllib.parse.urlencode({"head": f"{self.owner}:{branch}", "state": "open"})
        found = self._call("GET", f"/pulls?{query}")
        if found:
            number = int(found[0]["number"])
            self._call("PATCH", f"/pulls/{number}", {"title": title, "body": body})
            return number, str(found[0]["html_url"])
        pr = self._call(
            "POST",
            "/pulls",
            {"title": title, "head": branch, "base": "main", "body": body, "draft": False},
        )
        return int(pr["number"]), str(pr["html_url"])

    def snapshot(self, pr_number: int) -> Pr:
        pr = self._call("GET", f"/pulls/{pr_number}")
        head = str(pr["head"]["sha"])
        runs = self._call("GET", f"/commits/{head}/check-runs?per_page=100")["check_runs"]
        comments = [
            Comment(int(c["id"]), c["user"]["login"], c["body"], c.get("path"), c.get("line"))
            for c in self._call("GET", f"/pulls/{pr_number}/comments?per_page=100")
        ]
        comments += [
            Comment(int(c["id"]), c["user"]["login"], c["body"], None, None)
            for c in self._call("GET", f"/issues/{pr_number}/comments?per_page=100")
        ]
        comments += [
            Comment(int(r["id"]), r["user"]["login"], r["body"], None, None)
            for r in self._call("GET", f"/pulls/{pr_number}/reviews?per_page=100")
            if (r.get("body") or "").strip()
        ]
        merged = bool(pr.get("merged"))
        return Pr(
            state=str(pr["state"]),
            merged=merged,
            head_sha=head,
            merge_head_sha=head if merged else None,
            checks=_roll_up(runs),
            comments=tuple(comments),
        )


_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _roll_up(runs: list[dict[str, Any]]) -> str:
    if any(r.get("conclusion") in _FAILED for r in runs):
        return "failure"
    if runs and all(r.get("status") == "completed" and r.get("conclusion") in _FINE for r in runs):
        return "success"
    return "pending"
