"""The hand-off through the shared /work volume: one request in, one result out.

The orchestrator writes `inbox/` (request.json and base.tar, the base commit's `git archive`) and
renames it into place. The sandbox runner takes it, deletes it before any agent starts, and later
writes `outbox/result.json`. A result counts only if it carries the job's random nonce, which only
the orchestrator and the runner's memory ever hold, so an agent can't plant one. Standard library
only: the sandbox imports this too.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from jarvis_server.improve.ports import StageRequest, StageResult

MAX_PATCH = 5 * 1024 * 1024
_INBOX, _OUTBOX = "inbox", "outbox"


class TooBig(Exception):
    """A patch larger than any honest stage writes."""


def write_request(work: Path, request: StageRequest, *, base_tar: bytes) -> str:
    """Clear the volume, write the request, and return its nonce."""
    for name in (_INBOX, _OUTBOX, f"{_INBOX}.new"):
        _remove(work / name)
    nonce = secrets.token_hex(16)
    new = work / f"{_INBOX}.new"
    new.mkdir(mode=0o700)
    body = {
        "nonce": nonce,
        "job_id": request.job_id,
        "ticket_id": request.ticket_id,
        "stage": request.stage,
        "base_sha": request.base_sha,
        "words": request.words,
        "evidence": dict(request.evidence),
        "design": request.design,
        "patches": [_b64(p) for p in request.patches],
        "feedback": list(request.feedback),
    }
    (new / "base.tar").write_bytes(base_tar)
    (new / "request.json").write_text(json.dumps(body))
    new.rename(work / _INBOX)
    return nonce


def take_request(work: Path) -> tuple[StageRequest, bytes, str] | None:
    """The request, the base archive and the nonce, removed from the volume; None if none."""
    inbox = work / _INBOX
    try:
        body = json.loads((inbox / "request.json").read_text())
        tar = (inbox / "base.tar").read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    finally:
        _remove(inbox)
    request = StageRequest(
        job_id=int(body["job_id"]),
        ticket_id=int(body["ticket_id"]),
        stage=str(body["stage"]),
        base_sha=str(body["base_sha"]),
        words=str(body["words"]),
        evidence=dict(body["evidence"]),
        design=body["design"],
        patches=tuple(_unb64(p) for p in body["patches"]),
        feedback=tuple(str(f) for f in body["feedback"]),
    )
    return request, tar, str(body["nonce"])


def clear_inbox(work: Path) -> None:
    """Remove anything an agent left where the next stage's request goes."""
    _remove(work / _INBOX)
    _remove(work / f"{_INBOX}.new")


def write_result(work: Path, *, job_id: int, nonce: str, result: StageResult) -> None:
    if len(result.patch) > MAX_PATCH:
        raise TooBig(f"the patch is {len(result.patch)} bytes; the limit is {MAX_PATCH}")
    _remove(work / _OUTBOX)
    _remove(work / f"{_OUTBOX}.new")
    new = work / f"{_OUTBOX}.new"
    new.mkdir(mode=0o700)
    body = {
        "job_id": job_id,
        "nonce": nonce,
        "ok": result.ok,
        "cost_usd": result.cost_usd,
        "turns": result.turns,
        "text": result.text,
        "design": result.design,
        "patch": _b64(result.patch),
        "blocked_urls": list(result.blocked_urls),
        "error": result.error,
    }
    (new / "result.json").write_text(json.dumps(body))
    new.rename(work / _OUTBOX)


def read_result(work: Path, *, job_id: int, nonce: str) -> StageResult | None:
    """This job's result, removed from the volume; None until one with the nonce is there."""
    path = work / _OUTBOX / "result.json"
    try:
        body: dict[str, Any] = json.loads(path.read_text())
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        return None
    if body.get("job_id") != job_id or not secrets.compare_digest(
        str(body.get("nonce", "")), nonce
    ):
        return None
    _remove(work / _OUTBOX)
    patch = _unb64(body.get("patch") or "")
    if len(patch) > MAX_PATCH:
        raise TooBig(f"the patch is {len(patch)} bytes; the limit is {MAX_PATCH}")
    return StageResult(
        ok=bool(body["ok"]),
        cost_usd=float(body["cost_usd"]),
        turns=int(body["turns"]),
        text=str(body.get("text") or ""),
        design=body.get("design"),
        patch=patch,
        blocked_urls=tuple(str(u) for u in body.get("blocked_urls") or ()),
        error=body.get("error"),
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()
