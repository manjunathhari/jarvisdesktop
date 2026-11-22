"""Single, audited consumption of an approval (spec §5.5, §9.4; R7-1, R10-1).

`ledger.consume_approval` issues an approval's execution key exactly once, only while the
approval is APPROVED and in date. A refusal is audited in the same transaction and returned as
a reason code; this wrapper runs the call through `dbcall.audited_call`, so the refusal's audit
row is committed before `ConsumeRefused` is raised.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from jarvis_server.dbcall import ConsumeRefused, audited_call

REFUSALS = ("unknown_approval", "not_approved", "expired", "already_consumed")


def consume(conn: psycopg.Connection[Any], approval_id: UUID) -> UUID:
    """Issue the approval's execution key (core role only); ConsumeRefused(reason) if refused."""
    key, refusal = audited_call(
        conn,
        "SELECT execution_key, refusal FROM ledger.consume_approval(%s)",
        (approval_id,),
    )
    if refusal is not None:
        raise ConsumeRefused(str(refusal))
    return UUID(str(key))
