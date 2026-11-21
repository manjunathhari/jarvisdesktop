# Security review — Phase 0 (server foundations)

**Date:** 2026-11-21 · **Scope:** migrations/SQL, Python services, containers, compose, hardening script, CI, shared Hostinger server · **Reviewer:** 10x-Team (Security)

## Threat model (short)
Assets: the ledger and its audit history; database credentials; the host. Adversaries: internet scanners and brute-forcers; a compromised neighbour on the shared box (Traefik, pubmedgemini — both internet-facing); a buggy or prompt-injected future agent holding the app role; leakage through the public repo.

## What holds
| Control | Verified by |
|---|---|
| Nothing JARVIS reachable from the internet; only TCP 22/80/443 open | P0-8 external scan |
| SSH: public key only, root by key only, fail2ban (Ubuntu 24.04 jail matches `_COMM=sshd`) | P0-8; `fail2ban-client status sshd` in runbook |
| Provider firewall outside the VM (Docker cannot bypass it) | Hostinger firewall `jarvis-shared-server`, synced |
| JARVIS publishes no host ports; internal Docker network without internet route | compose; container list |
| App role cannot change state except via `ledger.transition()`, cannot delete ledger rows, cannot read-modify or forge audit rows, cannot call the internal audit writer | CI privilege tests + live P0-3 |
| Owner role cannot edit/delete/truncate audit rows (trigger) | CI |
| SECURITY DEFINER functions pin `search_path = pg_catalog, pg_temp`; dynamic SQL only via `format('%I')` over a two-name whitelist; target state checked against `allowed_transition` before any cast | Code review of 0004 |
| `PUBLIC` loses EXECUTE on internal functions, CONNECT/TEMP on the database, CREATE on `public` | Code review of bootstrap + 0003/0004 |
| Secrets only in root:10001 0440 files, mounted per service on a need-to-know basis (core/scheduler: app password only); never in env vars, Hostinger's env field or the repo | compose; `config.py`; repo grep for IP/hostname/server ID |
| Containers: non-root uid 10001, read-only root fs, `cap_drop: ALL`, `no-new-privileges`, memory limits | compose |
| Hardening script fetched by commit and verified by SHA-256 before running | Command used 21 Nov |

## Findings
| # | Severity | Finding | Decision |
|---|---|---|---|
| S1 | Medium (accepted) | SSH (22) stays internet-facing on the shared server because pubmedgemini's deploy uses it (Manj chose to leave pubmedgemini untouched) | Accepted; key-only + fail2ban; closes when JARVIS moves to its own server (runbook §3) |
| S2 | Medium (accepted) | Shared host: a compromise of Traefik/pubmedgemini gives a foothold on the same VM | Accepted with K1; JARVIS isolated by network, no published ports, separate secrets; trigger to move: JARVIS functioning |
| S3 | Low | Actor identity on writes is declared by the caller (`set_config('jarvis.actor_*')`); any holder of the app role can claim any actor | Accepted for Phase 0 (single trusted app). Phase 2 gateway must derive actor from authenticated node/person identity, not from client input |
| S4 | Low | Images referenced by tag (`postgres:17`, `postgres-backup-local:17`, GitHub Actions by version) not by digest | Recommend pinning digests before the move to a dedicated server |
| S5 | Low | `jarvis_app` can connect to the default `postgres` database (empty, no privileges) | Accepted; revoke `CONNECT ON DATABASE postgres FROM PUBLIC` at next migration touch |
| S6 | Low | Superuser can disable audit triggers; superuser password file is mounted only into `init` | Accepted (ADR-008); hash chain deferred with trigger "second admin" |
| S7 | Info | Postgres container trusts local-socket connections inside the container | Standard image behaviour; only processes inside the postgres container can use it |
| S8 | Info | Repo is public | No secrets/addresses committed (checked); scripts take them as arguments; server IP is a repository secret |

## Sign-off
No blocking issues. Accepted risks are tied to the temporary shared server and close when JARVIS moves to its own server.
