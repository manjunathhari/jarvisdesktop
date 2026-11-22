# QA report — Phase 1 (memory engine)

**Date:** 2026-11-22 · **Branch:** `phase-1-memory-engine` · **Reviewer:** independent agent (did not write the code), then fixes by 10x-Team

## Acceptance tests
| # | Test | Evidence | Result |
|---|---|---|---|
| M1 | Write then retrieve a preference: same entry, ID, source | `test_m1_*` (Postgres) + deploy check | Pass |
| M2 | Update: new git version, old recoverable | `test_m2_*` + deploy check; `git show` of the old commit | Pass |
| M3 | Inferred entry blocked until confirmed | `test_m3_*`: agent and system confirms refused and audited; Manj's confirm unblocks | Pass |
| M4 | `retrieve_context(daughter, school trip)` returns only relevant, permitted entries | `test_m4_*`: each exclusion checked by name (not relevant, not permitted, expired, other subject) | Pass |

## Test suite
**236 tests, all passing** locally (Postgres 16): Phase 0's 72 plus 164 for memory: format 50, engine 74, Postgres acceptance 29, service 4, contracts +7. Ruff, format and mypy (strict) clean. CI runs Postgres 17.

## Findings
| # | Severity | Finding | Action |
|---|---|---|---|
| Q1 | Medium | Episodic year < 1000 wrote a file the engine then refused to load (core would not start) | Fixed: four-digit years only; test |
| Q2 | Medium | A failed `git commit` left files staged; the next change committed them too (duplicate IDs) | Fixed: any failure resets and cleans the tree; test with a simulated failure |
| Q3 | Medium | Matching only saw ASCII: Kannada/Devanagari content never found; "café" ≠ "cafe" | Fixed: any script, NFKC, Latin accents dropped; tests |
| Q4 | Low | Malformed ID or slug raised a database error instead of an audited refusal | Fixed: such refusals are audited as `m-unassigned`; Postgres test |
| Q5 | Low | No test of "most recent first"; gaps for the above | Fixed: tests added for every finding |
| Q6 | Info | `history` is one `git show` + parse per commit touching the file | Accepted; fine at Phase 1 size |
| Q7 | Info | Production took `HOUSEHOLD_ID` from the test-fixture module | Fixed: `config.HOUSEHOLD_ID` |
| D1–D6 | Info | Spec deviations (household edit rule, UUID v3, bundle volume, `recorded_on`, first commit, limit/arguments) | Recorded in spec §15; D6 fixed |

## Not verified here
The Docker image build and a live deploy (the build session cannot reach Docker Hub; CI builds and publishes the image), and the runbook's `alpine/git` restore command (the bundle → clone → engine path is tested).

## Release readiness
Ready for PR and pre-merge live check. Phase 1 boundaries hold: no embeddings, AI, consolidation, external services or real personal data.
