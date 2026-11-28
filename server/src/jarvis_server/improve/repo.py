"""The orchestrator's own clone of the repo: the base, the archive, inspecting a patch, pushing.

A patch comes from the sandbox and is untrusted. It is checked with `git apply --numstat` and
`--summary` before anything is written, applied only in a fresh worktree of this clone, and nothing
it contains is ever run: no hooks (core.hooksPath=/dev/null), no fsmonitor, no filters or textconv.
Pushes go only to improve/** branches; the GitHub App's ruleset holds that line on GitHub too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from jarvis_server.improve import gate
from jarvis_server.improve.ports import Inspected

_SAFE = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.autocrlf=false",
)
_SPECIAL_MODE = re.compile(r"^ (?:create|delete) mode (120000|160000) (.+)$")
_MODE_CHANGE = re.compile(r"^ mode change \d+ => (120000|160000) (.+)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class RepoError(Exception):
    """git refused, or the request was outside what the team may do."""


class GitRepo:
    def __init__(
        self,
        *,
        origin: str,
        clone: Path,
        env: Mapping[str, str] | None = None,
        auth: Callable[[], str] | None = None,
    ) -> None:
        self.origin, self.clone = origin, clone
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "JARVIS improve",
            "GIT_AUTHOR_EMAIL": "improve@jarvis.invalid",
            "GIT_COMMITTER_NAME": "JARVIS improve",
            "GIT_COMMITTER_EMAIL": "improve@jarvis.invalid",
            **(env or {}),
        }
        self.auth = auth  # gives "AUTHORIZATION: basic …"; passed by env, never in a URL

    def _git(
        self,
        *args: str,
        tree: Path | None = None,
        index: Path | None = None,
        stdin: bytes | None = None,
        bare: bool = True,
    ) -> bytes:
        env = dict(self.env)
        if self.auth is not None and args[0] in ("clone", "fetch", "push"):
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraheader",
                GIT_CONFIG_VALUE_0=self.auth(),
            )
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        where = ["--git-dir", str(self.clone)] if bare else []
        if tree is not None:
            where += ["--work-tree", str(tree)]
        done = subprocess.run(  # noqa: S603 - fixed git arguments, no shell
            ["git", *_SAFE, *where, *args],  # noqa: S607
            cwd=tree or self.clone.parent,
            input=stdin,
            capture_output=True,
            env=env,
            check=False,
        )
        if done.returncode != 0:
            raise RepoError(f"git {args[0]}: {done.stderr.decode(errors='replace')[-500:]}")
        return done.stdout

    def _fetch(self) -> None:
        if not (self.clone / "HEAD").exists():
            self.clone.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                "clone", "-q", "--bare", "--filter=blob:none", self.origin, str(self.clone),
                bare=False,
            )  # fmt: skip
        self._git("fetch", "-q", "--prune", self.origin, "+refs/heads/main:refs/heads/main")

    def base_sha(self) -> str:
        self._fetch()
        return self._git("rev-parse", "refs/heads/main^{commit}").decode().strip()

    def archive(self, sha: str, dest: Path) -> None:
        _check_sha(sha)
        dest.write_bytes(self._git("archive", "--format=tar", sha))

    def _worktree(self, sha: str) -> tuple[Path, Path]:
        """A fresh checkout of `sha` with its own index, outside the clone."""
        _check_sha(sha)
        tree = Path(tempfile.mkdtemp(prefix="improve-tree-"))
        index = Path(tempfile.mkdtemp(prefix="improve-index-")) / "index"
        self._git("read-tree", sha, index=index)
        self._git("checkout-index", "-a", "-f", tree=tree, index=index)
        return tree, index

    def _apply(
        self, tree: Path, index: Path, patch: bytes
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Check the patch, then apply it to `tree`; the changed and the special paths."""

        def apply(*extra: str) -> bytes:
            return self._git(
                "apply",
                "--binary",
                *extra,
                "-",
                tree=tree,
                index=index,
                stdin=patch,
            )

        numstat = apply("--numstat", "-z")
        summary = apply("--summary")
        changed = tuple(
            entry.split("\t", 2)[2]
            for entry in numstat.decode(errors="replace").split("\0")
            if entry.count("\t") >= 2
        )
        special = []
        for line in summary.decode(errors="replace").splitlines():
            m = _SPECIAL_MODE.match(line) or _MODE_CHANGE.match(line)
            if m:
                special.append(m.group(2))
        for path in changed:
            if not gate.is_plain(path):
                raise RepoError(f"{path!r} is not a plain repo path")
        apply()
        return changed, tuple(special)

    def inspect(self, sha: str, patch: bytes, wanted: Sequence[str]) -> Inspected:
        if not patch:
            return Inspected((), (), {}, {}, "")
        tree, index = self._worktree(sha)
        try:
            changed, special = self._apply(tree, index, patch)
            locks = {}
            for path in changed:
                if PurePosixPath(path).name in gate.LOCK_FILES and path not in special:
                    new_file = tree / path
                    new = new_file.read_text() if new_file.is_file() else ""
                    locks[path] = (self._show(sha, path), new)
            hashes = {}
            for path in wanted:
                f = tree / path
                if gate.is_plain(path) and f.is_file() and not f.is_symlink():
                    hashes[path] = gate.fingerprint(f.read_bytes())
            return Inspected(changed, special, locks, hashes, patch.decode(errors="replace"))
        finally:
            _cleanup(tree, index)

    def _show(self, sha: str, path: str) -> str:
        try:
            return self._git("show", f"{sha}:{path}").decode(errors="replace")
        except RepoError:
            return ""

    def push(self, sha: str, patch: bytes, branch: str, message: str) -> str:
        if not branch.startswith("improve/") or not gate.is_plain(branch):
            raise RepoError(f"the team pushes only to improve/ branches, not {branch!r}")
        tree, index = self._worktree(sha)
        try:
            self._apply(tree, index, patch)
            self._git("add", "-A", "--", ".", tree=tree, index=index)
            tree_id = self._git("write-tree", index=index).decode().strip()
            head = self._git("commit-tree", tree_id, "-p", sha, "-m", message).decode().strip()
            self._git("push", "-q", "--force", self.origin, f"{head}:refs/heads/{branch}")
            return head
        finally:
            _cleanup(tree, index)


def _cleanup(tree: Path, index: Path) -> None:
    shutil.rmtree(tree, ignore_errors=True)
    shutil.rmtree(index.parent, ignore_errors=True)


def _check_sha(sha: str) -> None:
    if not _SHA.match(sha):
        raise RepoError(f"{sha!r} is not a commit id")
