"""Memory inside core (spec §9, §10 live check): open, daily bundle, restore, deploy check."""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from jarvis_server.memory import checks, service
from jarvis_server.memory.engine import MemoryEngine
from jarvis_server.memory.errors import RemoteConfigured


def engine(root: Path) -> MemoryEngine:
    e = MemoryEngine(root, checks.StaticDirectory(), checks.ListAudit(), checks.HOUSEHOLD_ID)
    return e.open()


def test_daily_bundle_restores_with_git_clone(tmp_path: Path) -> None:
    eng = engine(tmp_path / "memory")
    checks.add_test_family(eng)
    ids = checks.seed_m4(eng)
    bundle = service.write_daily_bundle(eng, tmp_path / "bundles", date(2026, 11, 22))
    assert bundle is not None and bundle.name == "memory-2026-11-22.bundle"
    assert service.write_daily_bundle(eng, tmp_path / "bundles", date(2026, 11, 22)) is None

    restored = tmp_path / "restored"
    subprocess.run(["git", "clone", "-q", str(bundle), str(restored)], check=True)
    subprocess.run(["git", "-C", str(restored), "remote", "remove", "origin"], check=True)
    back = engine(restored)
    got = {e.memory_id for e in back.retrieve_context("daughter", "school trip", viewer="manj")}
    assert got == {ids[k] for k in checks.M4_EXPECTED}


def test_only_the_newest_bundles_are_kept(tmp_path: Path) -> None:
    eng = engine(tmp_path / "memory")
    start = date(2026, 11, 1)
    for i in range(10):
        service.write_daily_bundle(eng, tmp_path / "b", start + timedelta(days=i))
    kept = sorted(p.name for p in (tmp_path / "b").glob("*.bundle"))
    assert len(kept) == service.KEEP_BUNDLES
    assert kept[0] == "memory-2026-11-04.bundle" and kept[-1] == "memory-2026-11-10.bundle"


def test_a_restored_clone_keeps_its_remote_until_removed(tmp_path: Path) -> None:
    """The runbook's restore step removes 'origin'; until then the engine refuses the repo."""
    eng = engine(tmp_path / "memory")
    bundle = service.write_daily_bundle(eng, tmp_path / "b", date(2026, 11, 22))
    assert bundle is not None
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bundle), str(clone)], check=True)
    with pytest.raises(RemoteConfigured):
        engine(clone)


def test_deploy_check_passes_without_touching_anything() -> None:
    results = checks.run()
    assert [name.split()[0] for name, _, _ in results] == ["M1", "M2", "M3", "M4"]
    assert all(ok for _, ok, _ in results), results
