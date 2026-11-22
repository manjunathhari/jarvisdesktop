# Security review — Phase 1 (memory engine)

**Date:** 2026-11-22 · **Scope:** `server/src/jarvis_server/memory/`, migration 0007, bootstrap S5, Dockerfile, compose, contracts · **Reviewer:** independent agent, then fixes by 10x-Team

## Threat model (short)
Assets: the memory (the most personal data Jarvis will hold) and its audit history. Adversaries: a buggy or prompt-injected future agent holding the app role or calling the engine; a person reading what they should not; anyone with write access to the memory volume (a compromised neighbour on the shared server, S2 from Phase 0); leakage through the public repo.

## What holds (verified)
| Control | Evidence |
|---|---|
| Owner's boundaries: no embeddings, AI, consolidation, external services; fictional people only | No new dependencies; fixtures and deploy check |
| PRIVATE = subject + recorder; **no automatic parental access** | `test_no_automatic_parental_access`, visibility matrix, reviewer probes |
| Hidden and missing entries look the same on every call | "no-such-entry" / `MemoryNotFound` on get, history, update, confirm, archive |
| Only a person confirms; agents/system write only INFERRED/HISTORICAL | Tag-rule table tests; M3 |
| No content in audit rows, commit messages or logs | Tests search the audit table and git log for a marker; database enforces code-only reasons |
| No git remote; refused at every start | Test |
| Git cannot be steered into running code: hooks off, fsmonitor off, no system/global config, extra local config or attributes refused, fixed argument lists, `--` before paths, paths from validated names only | Tests; code review |
| `audit.record_memory`: SECURITY DEFINER, pinned `search_path`, EXECUTE for `jarvis_app` only, app cannot insert or call `audit.append` | Tests |
| Memory volumes mounted only into `core`, uid 10001, mode 0700, read-only root fs | compose, Dockerfile |
| No slow regexes; content capped at 500 characters | Review |

## Findings
| # | Severity | Finding | Decision |
|---|---|---|---|
| S1 | Medium | Free text (archive reasons, refusal messages echoing arguments, `unknown:<by>` actor) could enter the **append-only** audit and never be erased | **Fixed**: reason codes only, enforced in SQL; archive takes a fixed code; no argument echo; actor `unknown` |
| S2 | Low | `core.fsmonitor` or filter drivers in the repo's own config could run code | **Fixed**: fsmonitor forced off; repos with extra local config or attributes refused |
| S3 | Low | The app role could record any tag change and any actor text through `record_memory` | **Fixed**: actor shape and legal tag change per action checked in SQL |
| S4 | Low | `template1` still allowed CONNECT for PUBLIC | **Fixed** in bootstrap |
| S5 | Info | Existence hints: sequential IDs reveal counts of hidden entries; agent confirm told existing from missing | Agent confirm **fixed**; ID gaps accepted (single household, IDs are not secret) |
| S7 | Info | CI publishes images from `phase-1-memory-engine`; bundle written without the repo lock | Remove the branch from `server.yml` after merge; single writer today |

## Carried forward
- **Before any real memory:** JARVIS moves to its own server (Manj, 2026-11-22); encryption at rest follows as hardening. Bundles currently sit on the same disk as the memory.
- S3 (Phase 0) still applies: the Phase 2 gateway must set `viewer`/`by` from authentication.

## Sign-off
No blocking issues. Phase 1 is safe to deploy with fictional data only, which is all it holds.
