# QA — index
## Active features
- phase-0-foundations — server foundations (72 tests; P0-7 deferred)
## Principles
- Test against real Postgres, never mocks, for anything touching privileges or SQL.
- Every deploy re-proves the core rules (smoke).
