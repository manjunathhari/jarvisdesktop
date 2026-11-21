# ADR-009: Shared VPS (temporary) — Tailscale-only access, file-based secrets, rebuild-from-repo recovery

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** phase-0-foundations
**Author:** 10x-Team (Architect + Security Engineer + DevOps + SRE)

## Context
Kick-off decision K1: JARVIS runs on Manj's existing Hostinger KVM 2 (Mumbai, Ubuntu 24.04 "Docker + Traefik" image), **shared** with:
- Traefik serving public sites on ports 80/443,
- pubmedgemini (app, its own Postgres 17, OAuth2 login gate) — public through Traefik, deployed over SSH as root with a deploy key,
- Ollama (local models),
- a standalone Postgres 16 bound to localhost, with a local backup container.

At kick-off: no Hostinger firewall configured, root login over SSH from the internet, one root SSH key (pubmedgemini's deploy key).

v2 asks for: Tailscale-only access, secrets never in the repo (and "in a vault"), append-only audit, encrypted off-site backups. Acceptance tests P0-7 (real restore) and P0-8 (nothing JARVIS reachable from the internet) depend on this ADR. The repo is **public**, so no addresses, hostnames, server IDs or secrets may be committed.

## Decision

### Network
1. **Tailscale** on the server and Manj's PC (one tailnet, Manj's account). The server gets tag `tag:server`; MagicDNS name used everywhere instead of IPs.
2. **Hostinger firewall** (outside the VM, so Docker cannot bypass it) attached to the server: allow inbound TCP 80 and 443 (pubmedgemini via Traefik) and UDP 41641 (Tailscale direct connections); drop everything else, **including TCP 22**.
3. **SSH:** key-only (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`), so pubmedgemini's existing root deploy key keeps working, but only over Tailscale. Manj's PC gets its own key.
4. **pubmedgemini deploy** moves onto the tailnet: its GitHub workflow uses `tailscale/github-action` with an OAuth client limited to `tag:ci` (ephemeral nodes), and an ACL allowing `tag:ci → tag:server:22` only. Verified with a real deploy **before** port 22 is closed.
5. **JARVIS publishes no host ports.** Its containers sit on an internal Docker network. In Phase 2 the gateway binds to the server's Tailscale address only.
6. **Ubuntu side:** unattended security upgrades on; fail2ban not needed once SSH is off the internet.
7. **Break-glass:** Hostinger web console (works with SSH closed) behind the Hostinger account's password + 2FA.

### Secrets
- Files under `/etc/jarvis/secrets/` (root:root, mode 0600), mounted into containers as Docker Compose `secrets` (they appear as files in `/run/secrets/`, never in environment variables or `docker inspect`).
- Contents in Phase 0: `pg_superuser_password`, `pg_owner_password`, `pg_app_password`.
- Generated on the server by `deploy/harden/` scripts; never typed into chat, never committed.
- Nothing secret has to survive losing the server: a rebuild generates new secrets.
- JARVIS secrets are **never** put in Hostinger's Docker manager environment field (its API returns those values in plain text).
- A real vault (e.g. SOPS + age, or Vaultwarden/Infisical) is **deferred**; trigger: OAuth refresh tokens for Gmail/Calendar arrive (Phase 3) or a second admin is added.

### Recovery and backups
Manj's requirement (21 Nov): **losing ledger data is acceptable; losing JARVIS itself is not.** And this shared server is **temporary** — JARVIS moves to its own server once it is functioning.

So recovery means *rebuild*, and rebuild is the same procedure as *moving out*:
- **Everything needed to recreate JARVIS lives in the repo:** code, migrations, compose file, hardening and deploy scripts, runbook. Images are in GitHub Container Registry. Nothing about the system exists only on the server.
- **Rebuild / move procedure** (`server/deploy/README.md`): new Ubuntu server → `harden/` scripts (generate fresh secrets) → Tailscale → `compose up` → `core migrate` → restore the latest dump if one is available. A fresh, empty JARVIS is a valid outcome.
- **Local data backups:** the same `postgres-backup-local` container already used on this server for `ascent`/`tracker`, pointed at the `jarvis` database: daily, keep 7 daily / 4 weekly / 3 monthly, on a Docker volume on the server.
- **Hostinger's weekly VM backup** stays on as a second, coarse copy.
- **Restore test (P0-7):** the latest local dump is restored into a scratch Postgres container and P0-1's ticket and audit rows are compared. This is also the rehearsal for moving the data to the new server.
- **No off-site copy in Phase 0** (deliberate deviation from v2 §13.3). Trigger to add encrypted off-site backups (restic to a second provider): the move to JARVIS's own server, or the first time losing the data would actually hurt.

### Admin access
- **Manj only** (handover question 6). No second admin in Phase 0; revisit with the vault trigger.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Dedicated second VPS for JARVIS | Clean isolation; firewall fully closed | Extra monthly cost | Manj chose to share (K1) |
| UFW only (no Hostinger firewall) | Configured from the box | Docker-published ports bypass UFW; a misconfigured compose file could expose a port | Provider firewall can't be bypassed from inside |
| Keep SSH public with fail2ban | No change to pubmedgemini deploy | Internet-facing SSH on a box holding family data | v2 requires Tailscale-only access |
| Tailscale SSH instead of OpenSSH | No keys to manage | Ties login to Tailscale ACLs; pubmedgemini's deploy key flow changes more | Plain OpenSSH over the tailnet is simpler to reason about |
| Secrets as `.env` file | Common | Values visible in `docker inspect` and process env | Compose file secrets avoid that |
| SOPS-encrypted secrets in the repo | Versioned | Public repo; key management overhead now | Deferred until Phase 3 |
| Encrypted off-site backups now (restic → R2/B2) | Data survives loss of server or account | Extra account, keys and a password to keep safe | Data loss is acceptable for now (Manj); added at the move to JARVIS's own server |
| WAL archiving / point-in-time recovery (pgBackRest) | Minute-level recovery | More setup | Daily dumps fit family scale; revisit when Jarvis runs daily life |

## Consequences
### Positive
- Nothing belonging to JARVIS, and no SSH, is reachable from the internet; pubmedgemini keeps working.
- JARVIS can be rebuilt on any server from the repo; moving out is a rehearsed, scripted procedure.
- Local backups reuse a container already proven on this server.
### Negative
- Shared box: a compromise of pubmedgemini or Traefik (both internet-facing) puts the JARVIS host at risk. Accepted with K1; containers isolated, JARVIS ports unpublished.
- Root login (key-only, tailnet-only) remains for pubmedgemini's deploy.
- Losing the server or the Hostinger account loses the ledger data (accepted by Manj). Otherwise up to 24 h can be lost between daily backups.
### Risks
- Lock-out during the firewall change → order: Tailscale verified from PC → pubmedgemini deploy verified via tailnet → then close 22. Web console as fallback.
- Rebuild steps rot if never run → the move to the new server is done with the same scripts; CI checks the compose file and migrations on every change.
- Docker group = root-equivalent → only root uses Docker on this box.

## Dependencies
- Depends on K1 (shared server), ADR-006 (Postgres), ADR-008 (backups as audit layer 3).
- Constrains Phase 2 (gateway binds to the Tailscale address; desktop reaches it over the tailnet) and Phase 3 (vault trigger).
- Needs from Manj: Tailscale account sign-in on server and PC; Hostinger account 2FA.
- Trigger to revisit: JARVIS starts functioning → move to its own server, close the firewall fully, add off-site backups.
