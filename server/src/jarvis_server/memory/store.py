"""The private git repo that holds the memory files (Phase 1 spec §4.4, ADR-010).

Uses the git command line with fixed argument lists (never a shell). System and global git
config are ignored and hooks are disabled, so nothing in the environment or the repo can run
code or add a remote behind the engine's back. The repo must never have a remote.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis_server.memory.errors import MemoryEngineError, RemoteConfigured, UnsafeRepo

AUTHOR = ("Jarvis", "jarvis@localhost")
_SAFE_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": AUTHOR[0],
    "GIT_AUTHOR_EMAIL": AUTHOR[1],
    "GIT_COMMITTER_NAME": AUTHOR[0],
    "GIT_COMMITTER_EMAIL": AUTHOR[1],
    "LC_ALL": "C",
}
_CONFIG = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false",
    "-c", "core.quotepath=off",
)  # fmt: skip
# The only keys allowed in the repo's own .git/config: anything else (fsmonitor, filters,
# include.path, url rewrites, remotes ...) could run code or send memory somewhere.
_ALLOWED_LOCAL_CONFIG = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.symlinks",
    }
)


class GitError(MemoryEngineError):
    """A git command failed."""


@dataclass(frozen=True)
class Commit:
    sha: str
    committed_at: datetime
    message: str


class GitStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    # --- plumbing ------------------------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> str:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root), **_SAFE_ENV}
        proc = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            ["git", *_CONFIG, *args],  # noqa: S607 - git from PATH inside the image
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
        return proc.stdout

    # --- lifecycle -----------------------------------------------------------------------

    def exists(self) -> bool:
        return (self.root / ".git").is_dir()

    def create(self, files: dict[str, str], message: str) -> str:
        """Create the repo with its first commit."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        return self.commit(files, message)

    def check_no_remote(self) -> None:
        remotes = self._git("remote").split()
        if remotes:
            raise RemoteConfigured(
                f"memory repo at {self.root} has git remote(s) {remotes}; memory must never "
                "be pushable. Remove the remote by hand before starting."
            )

    def check_safe(self) -> None:
        """Refuse a repo whose local config or attributes go beyond the plain defaults."""
        keys = {
            k.lower()
            for k in self._git("config", "--local", "--name-only", "--list").split()
            if k.strip()
        }
        extra = sorted(keys - _ALLOWED_LOCAL_CONFIG)
        if extra:
            raise UnsafeRepo(f"memory repo has unexpected git config keys: {', '.join(extra)}")
        attributes = self.root / ".git" / "info" / "attributes"
        if attributes.exists() and attributes.read_text().strip():
            raise UnsafeRepo("memory repo has .git/info/attributes; remove it")

    def discard_uncommitted(self) -> bool:
        """Drop changes that were never committed (a write that crashed). True if any."""
        if not self._git("status", "--porcelain", "--untracked-files=all").strip():
            return False
        self._git("reset", "-q", "--hard", "HEAD")
        self._git("clean", "-q", "-fd")
        return True

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive lock for one change, shared by every process using this repo."""
        lock_path = self.root / ".git" / "jarvis.lock"
        with open(lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # --- reading -------------------------------------------------------------------------

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def read_files(self) -> dict[str, str]:
        """Every file in the working tree (outside .git), by repo-relative path."""
        out: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if rel.parts[0] == ".git" or not path.is_file():
                continue
            out[rel.as_posix()] = path.read_text(encoding="utf-8")
        return out

    def log(self, path: str | None = None) -> list[Commit]:
        """Commits, oldest first, optionally only those touching `path`."""
        args = ["log", "--reverse", "--format=%H%x1f%cI%x1f%B%x1e"]
        if path is not None:
            args += ["--", path]
        out: list[Commit] = []
        for record in self._git(*args).split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, when, message = record.split("\x1f", 2)
            out.append(Commit(sha, datetime.fromisoformat(when), message.strip()))
        return out

    def show(self, sha: str, path: str) -> str | None:
        out = subprocess.run(  # noqa: S603
            ["git", *_CONFIG, "show", f"{sha}:{path}"],  # noqa: S607
            cwd=self.root,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root),
                **_SAFE_ENV,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout if out.returncode == 0 else None

    # --- writing -------------------------------------------------------------------------

    def commit(self, files: dict[str, str], message: str) -> str:
        """Write `files` (path -> full text) and commit them. Returns the new commit."""
        before = self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
        try:
            for rel, text in files.items():
                target = self.root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, target)
            self._git("add", "--", *files)
            self._git("commit", "-q", "--no-verify", "-m", message)
        except BaseException:
            # Leave nothing staged or half-written for the next change to pick up (QA Q2).
            if before is not None:
                self._git("reset", "-q", "--hard", before, check=False)
            else:
                self._git("read-tree", "--empty", check=False)
            self._git("clean", "-q", "-fdx", "-e", ".git", check=False)
            raise
        return self.head()

    def bundle(self, target: Path) -> None:
        """Write a git bundle of every branch to `target` (daily backup; restore with clone)."""
        tmp = target.with_suffix(".tmp")
        self._git("bundle", "create", "-q", str(tmp), "--all")
        os.replace(tmp, target)

    def reset_to(self, sha: str) -> None:
        """Undo the engine's own last commit (audit failed). Only under the lock."""
        self._git("reset", "-q", "--hard", sha)
