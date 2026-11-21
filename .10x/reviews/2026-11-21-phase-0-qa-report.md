# QA report — Phase 0 (server foundations)

**Date:** 2026-11-21 · **Branch:** `phase-0-foundations` (PR #2) · **Reviewer:** 10x-Team (QA)

## Acceptance tests
| # | Test | Evidence | Result |
|---|---|---|---|
| P0-1 | Ticket walks DETECTED→…→COMPLETED, 7 audit rows in order | CI (Postgres 17) + live `SMOKE PASS` at deploy | Pass |
| P0-2 | DETECTED→COMPLETED refused, state unchanged, REJECTED row | CI + live | Pass |
| P0-3 | App role cannot UPDATE/DELETE/TRUNCATE/INSERT audit rows; owner blocked by trigger | CI (8 cases) + live | Pass |
| P0-4 | Commitment EXPECTED→AT_RISK→SECURED→FULFILLED, 4 rows; no exit from FULFILLED | CI + live | Pass |
| P0-5 | Expired and future context excluded from `current_context` | CI + live | Pass |
| P0-6 | Empty DB → head → base → head, identical schema | CI | Pass |
| P0-7 | Restore drill | — | **Deferred by Manj** (Hostinger weekly backup suffices until the build grows; procedure in `server/deploy/README.md` §4) |
| P0-8 | Only TCP 22/80/443 public; SSH publickey only | `network-check.yml` run from GitHub (outside tailnet) | Pass; re-runs weekly |

## Test suite
72 tests, all passing locally (Postgres 16) and in CI (Postgres 17): contracts (35), acceptance + privileges (21), lifecycle rules vs spec and enum parity (9), job queue + heartbeat + smoke (7).

Coverage highlights: every allowed and forbidden transition is checked against an independent copy of spec §6; DB enums equal contract enums; a privilege test proves no `state`/`id`/timestamp column is updatable by the app role; concurrent claim of 40 jobs by 4 workers yields 40 distinct claims.

## Findings
| # | Severity | Finding | Action |
|---|---|---|---|
| Q1 | Fixed | Commitment state protection was untested (only task) | Added two tests |
| Q2 | Fixed | Runbook used a raw `alembic downgrade` that lacked the DB address | Added `jarvis-core downgrade`; runbook updated |
| Q3 | Low | Every deploy's smoke run leaves ~3 labelled rows (`source_ref='smoke-check'`) in the live ledger | Accepted for Phase 0; `JARVIS_SMOKE_ON_INIT=0` turns it off. Revisit when real data arrives (Phase 2/3) |
| Q4 | Low | Local tests run on Postgres 16, server on 17 | CI runs 17 on every change; no version-specific features used |
| Q5 | Info | Backup container relied on its default dump options | Pinned `POSTGRES_EXTRA_OPTS=-Z1` (all schemas) |

## Release readiness
Ready to merge. P0-7 deferred by explicit decision, not a defect.
