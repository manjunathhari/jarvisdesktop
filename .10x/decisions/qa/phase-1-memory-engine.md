# QA — phase-1-memory-engine
- Strategy: format tests (no git/DB), engine rules with in-memory fakes, M1–M4 + audit against real Postgres in its own database (keeps P0-6 independent), service tests (bundle → clone → engine). Deploy smoke runs M1–M4 in a temp repo.
- Gates: M1–M4 green in CI and in `init` logs; P0-1…P0-6 stay green; ruff/format/mypy clean.
- Report: `.10x/reviews/2026-11-22-phase-1-qa-report.md`. 236 tests.
