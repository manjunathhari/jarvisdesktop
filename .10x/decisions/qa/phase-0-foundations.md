# QA — phase-0-foundations
- Strategy: contract tests → DB-level acceptance tests (P0-1…P0-6) against real Postgres → rules generated from an independent copy of the spec tables → live smoke on every deploy (P0-1…P0-5) → external network scan (P0-8), weekly.
- Status 2026-11-21: 72 tests green (CI on Postgres 17); P0-1…P0-6 and P0-8 pass; P0-7 deferred by Manj.
- Quality gates: CI `test` must pass before `publish`; `init` smoke must pass before core/scheduler start.
- Report: `.10x/reviews/2026-11-21-phase-0-qa-report.md`.
