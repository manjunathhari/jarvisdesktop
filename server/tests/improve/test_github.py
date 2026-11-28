"""The GitHub App client, against a scripted GitHub (no network)."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from jarvis_server.improve.github import GitHubApp, GitHubError

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()


class Scripted:
    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: Any) -> Any:
        self.calls.append((method, url, headers, body))
        path = url.removeprefix("https://api.github.com")
        key = (method, path.split("?")[0])
        if key not in self.routes:
            raise AssertionError(f"unexpected {method} {path}")
        answer = self.routes[key]
        return answer(path, body) if callable(answer) else answer


def _app(
    routes: dict[tuple[str, str], Any], now: float = 1_800_000_000.0
) -> tuple[GitHubApp, Scripted]:
    routes = {
        ("POST", "/app/installations/55/access_tokens"): {
            "token": "ghs_installationtokenvalue000000000000",
            "expires_at": "2027-01-15T09:00:00Z",
        },
        **routes,
    }
    http = Scripted(routes)
    app = GitHubApp(
        app_id="123",
        installation_id="55",
        private_key_pem=PEM,
        repo="manjunathhari/jarvisdesktop",
        send=http,
        clock=lambda: now,
    )
    return app, http


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_the_app_signs_a_short_jwt_and_asks_only_for_this_repo() -> None:
    app, http = _app({})
    assert app.token() == "ghs_installationtokenvalue000000000000"
    _, _, headers, body = http.calls[0]
    jwt = headers["Authorization"].removeprefix("Bearer ")
    head, claims, sig = jwt.split(".")
    KEY.public_key().verify(
        _b64(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    c = json.loads(_b64(claims))
    assert c["iss"] == "123" and c["exp"] - c["iat"] <= 600
    assert body["repositories"] == ["jarvisdesktop"]
    assert body["permissions"] == {"contents": "write", "pull_requests": "write", "checks": "read"}
    app.token()
    assert len(http.calls) == 1  # cached until near expiry


def test_the_push_header_carries_the_token_not_the_url() -> None:
    app, _ = _app({})
    header = app.git_auth_header()
    assert header.startswith("AUTHORIZATION: basic ")
    assert base64.b64decode(header.split()[-1]).decode() == (
        "x-access-token:ghs_installationtokenvalue000000000000"
    )


def test_a_pr_is_opened_once_then_updated() -> None:
    state: dict[str, Any] = {"open": []}

    def listing(path: str, body: Any) -> Any:
        assert "head=manjunathhari%3Aimprove%2F4-research" in path and "state=open" in path
        return state["open"]

    def create(path: str, body: Any) -> Any:
        assert body["base"] == "main" and body["head"] == "improve/4-research"
        assert body.get("maintainer_can_modify") is not True
        pr = {"number": 12, "html_url": "https://github.com/manjunathhari/jarvisdesktop/pull/12"}
        state["open"] = [pr]
        return pr

    app, http = _app(
        {
            ("GET", "/repos/manjunathhari/jarvisdesktop/pulls"): listing,
            ("POST", "/repos/manjunathhari/jarvisdesktop/pulls"): create,
            ("PATCH", "/repos/manjunathhari/jarvisdesktop/pulls/12"): {"number": 12},
        }
    )
    url = "https://github.com/manjunathhari/jarvisdesktop/pull/12"
    assert app.open_or_update_pr("improve/4-research", "Better research", "body") == (12, url)
    assert app.open_or_update_pr("improve/4-research", "Better research", "body 2") == (12, url)
    assert [c[0] for c in http.calls].count("POST") == 2  # the token, then one PR
    assert http.calls[-1][3] == {"title": "Better research", "body": "body 2"}


def test_the_app_never_opens_a_pr_from_another_branch() -> None:
    app, _ = _app({})
    with pytest.raises(GitHubError, match="improve/"):
        app.open_or_update_pr("main", "x", "y")


SHA = "b" * 40


def _snapshot_routes(
    checks: list[dict[str, Any]], merged: bool = False
) -> dict[tuple[str, str], Any]:
    base = "/repos/manjunathhari/jarvisdesktop"
    return {
        ("GET", f"{base}/pulls/12"): {
            "state": "closed" if merged else "open",
            "merged": merged,
            "head": {"sha": SHA},
        },
        ("GET", f"{base}/commits/{SHA}/check-runs"): {"check_runs": checks},
        ("GET", f"{base}/pulls/12/comments"): [
            {
                "id": 1,
                "user": {"login": "manjunathhari"},
                "body": "rename it",
                "path": "a.py",
                "line": 3,
            }
        ],
        ("GET", f"{base}/issues/12/comments"): [
            {"id": 2, "user": {"login": "someone"}, "body": "drive-by"}
        ],
        ("GET", f"{base}/pulls/12/reviews"): [
            {"id": 3, "user": {"login": "manjunathhari"}, "body": "", "state": "APPROVED"},
            {
                "id": 4,
                "user": {"login": "manjunathhari"},
                "body": "one more",
                "state": "CHANGES_REQUESTED",
            },
        ],
    }


@pytest.mark.parametrize(
    ("runs", "want"),
    [
        ([{"status": "completed", "conclusion": "success"}], "success"),
        (
            [
                {"status": "completed", "conclusion": "skipped"},
                {"status": "completed", "conclusion": "success"},
            ],
            "success",
        ),
        ([{"status": "in_progress", "conclusion": None}], "pending"),
        ([], "pending"),
        (
            [
                {"status": "completed", "conclusion": "failure"},
                {"status": "queued", "conclusion": None},
            ],
            "failure",
        ),
        ([{"status": "completed", "conclusion": "timed_out"}], "failure"),
    ],
)
def test_checks_roll_up(runs: list[dict[str, Any]], want: str) -> None:
    app, _ = _app(_snapshot_routes(runs))
    assert app.snapshot(12).checks == want


def test_the_snapshot_keeps_who_wrote_each_comment_and_skips_empty_reviews() -> None:
    app, _ = _app(_snapshot_routes([{"status": "completed", "conclusion": "success"}], merged=True))
    pr = app.snapshot(12)
    assert pr.merged and pr.merge_head_sha == SHA and pr.head_sha == SHA
    assert [(c.id, c.author, c.path, c.line) for c in pr.comments] == [
        (1, "manjunathhari", "a.py", 3),
        (2, "someone", None, None),
        (4, "manjunathhari", None, None),
    ]


def test_the_client_has_no_merge() -> None:
    assert not [
        name for name in dir(GitHubApp) if "merge" in name.lower() or "approve" in name.lower()
    ]
