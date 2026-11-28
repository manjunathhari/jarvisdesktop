"""The orchestrator's own clone: archive, inspect and push, against a local bare repo."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from jarvis_server.improve import gate
from jarvis_server.improve.repo import GitRepo, RepoError

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("jarvis\n")
    (seed / "server").mkdir()
    (seed / "server/uv.lock").write_text('[[package]]\nname = "httpx"\nversion = "0.28.1"\n')
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


@pytest.fixture
def repo(origin: Path, tmp_path: Path) -> GitRepo:
    return GitRepo(origin=str(origin), clone=tmp_path / "clone", env=ENV)


def _patch(
    origin: Path, tmp_path: Path, edit: dict[str, str | None], link: str | None = None
) -> bytes:
    work = tmp_path / "w"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    for path, text in edit.items():
        p = work / path
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    if link:
        (work / link).symlink_to("/etc/passwd")
    _git(work, "add", "-A")
    return subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=work, env=ENV, check=True, capture_output=True
    ).stdout


def test_the_base_is_main_and_the_archive_is_its_tree(
    repo: GitRepo, origin: Path, tmp_path: Path
) -> None:
    sha = repo.base_sha()
    assert sha == _git(origin, "rev-parse", "main")
    dest = tmp_path / "base.tar"
    repo.archive(sha, dest)
    with tarfile.open(dest) as tar:
        assert {"README.md", "server/uv.lock"} <= set(tar.getnames())


def test_inspect_lists_the_changes_hashes_and_lock_texts(
    repo: GitRepo, origin: Path, tmp_path: Path
) -> None:
    patch = _patch(
        origin,
        tmp_path,
        {
            "server/tests/test_x.py": "def test(): ...\n",
            "server/uv.lock": '[[package]]\nname = "httpx"\nversion = "0.28.2"\n',
        },
    )
    seen = repo.inspect(repo.base_sha(), patch, ["server/tests/test_x.py", "missing.py"])
    assert set(seen.changed) == {"server/tests/test_x.py", "server/uv.lock"}
    assert seen.special == ()
    assert seen.hashes == {"server/tests/test_x.py": gate.fingerprint(b"def test(): ...\n")}
    old, new = seen.lock_texts["server/uv.lock"]
    assert "0.28.1" in old and "0.28.2" in new
    assert "+def test(): ..." in seen.diff


def test_inspect_reports_a_symlink_as_special(repo: GitRepo, origin: Path, tmp_path: Path) -> None:
    patch = _patch(origin, tmp_path, {}, link="evil")
    assert repo.inspect(repo.base_sha(), patch, []).special == ("evil",)


def test_a_patch_that_does_not_apply_is_refused(repo: GitRepo) -> None:
    with pytest.raises(RepoError):
        repo.inspect(
            repo.base_sha(),
            b"diff --git a/nope b/nope\n--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-a\n+b\n",
            [],
        )


def test_push_goes_to_the_improve_branch_only(repo: GitRepo, origin: Path, tmp_path: Path) -> None:
    patch = _patch(origin, tmp_path, {"README.md": "jarvis, better\n"})
    base = repo.base_sha()
    head = repo.push(base, patch, "improve/4-research", "improve: research (ticket 4)")
    assert _git(origin, "rev-parse", "improve/4-research") == head
    assert _git(origin, "rev-parse", "main") == base  # main untouched
    assert _git(origin, "log", "-1", "--format=%s", head) == "improve: research (ticket 4)"
    again = repo.push(base, patch, "improve/4-research", "improve: research (ticket 4)")
    assert _git(origin, "rev-parse", "improve/4-research") == again
    with pytest.raises(RepoError, match="improve/"):
        repo.push(base, patch, "main", "x")
