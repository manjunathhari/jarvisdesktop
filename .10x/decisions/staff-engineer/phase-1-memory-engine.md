# Staff engineer — phase-1-memory-engine
- Follow Phase 0 patterns: psycopg 3 plain SQL, Actor-style attribution, SECURITY DEFINER for audit writes, contracts in `/contracts` mirrored by pydantic with example tests, enum parity tests.
- Memory content never appears in audit rows, commit messages or logs.
- git via subprocess with fixed argument lists (no shell), `GIT_CONFIG_NOSYSTEM=1`, explicit author/committer.
- mypy strict + ruff clean; tests use tmp git repos + the real Postgres fixture.
