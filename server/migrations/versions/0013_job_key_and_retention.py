"""Job deduplication and job retention (spec §5.4, §5.7; R12-32; plan Task 14, addition 1).

- `ops.job_one_per_key`: a unique partial index on `ops.job (kind, (payload->>'job_key'))
  WHERE payload ? 'job_key'`. At most one job per due item is now a database guarantee:
  `jobs.enqueue_once` inserts with ON CONFLICT DO NOTHING, and a conflict means "already
  queued". Jobs without a `job_key` are not affected.
- `ops.retention_sweep(p_now)` is replaced: the 0012 function plus `ops.job`. DONE and FAILED
  jobs are deleted 30 days after `finished_at`, with one more RETENTION audit row (table
  `ops.job`). QUEUED and RUNNING jobs are never deleted, and neither is a job that a kept
  `ops.brain_reservation` still references (`job_id`, R12-30). Jobs are swept after the
  reservations, so a reservation's job goes in the same run as the reservation. Grants,
  owner, SECURITY DEFINER and search_path are as in 0012.

Downgrade drops the index and restores the 0012 function exactly (read from 0012 itself).
RETENTION audit rows for `ops.job` stay: the audit trail is append-only, and the 0012 function
never reads them.

Revision ID: 0013
Revises: 0012
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

INDEX = """
CREATE UNIQUE INDEX job_one_per_key ON ops.job (kind, (payload->>'job_key'))
    WHERE payload ? 'job_key';
"""

# Inserted before the audit loop of the 0012 function, after the reservations are deleted.
JOBS = r"""
    -- ops.job: finished jobs, 30 days after they finished; never QUEUED or RUNNING, and never
    -- one a kept reservation still references (R12-32, R12-30).
    DELETE FROM ops.job j
    WHERE j.status IN ('DONE', 'FAILED') AND j.finished_at <= v_month
      AND NOT EXISTS (SELECT 1 FROM ops.brain_reservation r WHERE r.job_id = j.id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_tables := v_tables || 'ops.job'::text; v_counts := v_counts || v_n;
    v_cutoffs := v_cutoffs || v_month;

    -- One audit row per table"""

_AUDIT_LOOP = "\n    -- One audit row per table"


def _0012() -> ModuleType:
    path = Path(__file__).with_name("0012_memory_visibility_retention.py")
    spec = importlib.util.spec_from_file_location("jarvis_migration_0012", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sweep(*, with_jobs: bool) -> str:
    sql: str = _0012().RETENTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    if with_jobs:
        assert sql.count(_AUDIT_LOOP) == 1
        sql = sql.replace(_AUDIT_LOOP, "\n" + JOBS, 1)
    return sql


def upgrade() -> None:
    op.execute(INDEX)
    op.execute(_sweep(with_jobs=True))


def downgrade() -> None:
    op.execute(_sweep(with_jobs=False))
    op.execute("DROP INDEX ops.job_one_per_key;")
