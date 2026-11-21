# ADR-009: Shared VPS — Tailscale-only access, file-based secrets, encrypted off-site backups

**Status:** Proposed
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
- Contents in Phase 0: `pg_superuser_password`, `pg_owner_password`, `pg_app_password`, `restic_password`, `backup_s3_access_key`, `backup_s3_secret_key`.
- Generated on the server by `deploy/harden/` scripts; never typed into chat, never committed.
- **Copies of `restic_password` and the backup storage keys** go into Manj's password manager — without them the off-site backups cannot be restored.
- A real vault (e.g. SOPS + age, or Vaultwarden/Infisical) is **deferred**; trigger: OAuth refresh tokens for Gmail/Calendar arrive (Phase 3) or a second admin is added.

### Backups
- **Tool:** restic (client-side encryption, deduplication, verified restores).
- **What:** nightly `pg_dump --format=custom` of the `jarvis` database, streamed into restic; later the memory git repo (Phase 1) joins the same job.
- **Where:** a bucket at a **second provider** — proposed **Cloudflare R2** (Manj's Cloudflare account is already connected to Claude), alternative Backblaze B2. Bucket credentials scoped to that one bucket.
- **Schedule and retention:** systemd timer at 03:30 IST; keep 7 daily, 4 weekly, 12 monthly; weekly `restic check`.
- **Restore drill (P0-7):** script restores the latest snapshot into a throwaway Postgres container and compares P0-1's ticket and audit rows. Run once in Phase 0, then monthly (v2 Phase 5 makes it routine).
- Hostinger's weekly VM backup stays on as an extra, not as the backup of record (same provider, not encrypted by us).

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
| Backups to the same Hostinger account | Easy | Provider or account loss takes backups too | v2 requires off-site |
| WAL archiving / point-in-time recovery (pgBackRest) | Minute-level recovery | More setup | Nightly dumps fit family scale; revisit when Jarvis runs daily life |

## Consequences
### Positive
- Nothing belonging to JARVIS, and no SSH, is reachable from the internet; pubmedgemini keeps working.
- Backups survive loss of the server or the Hostinger account, and are proven by a real restore.
### Negative
- Shared box: a compromise of pubmedgemini or Traefik (both internet-facing) puts the JARVIS host at risk. Accepted with K1; containers isolated, JARVIS ports unpublished.
- Root login (key-only, tailnet-only) remains for pubmedgemini's deploy.
- Up to 24 h of ledger data can be lost between nightly backups.
### Risks
- Lock-out during the firewall change → order: Tailscale verified from PC → pubmedgemini deploy verified via tailnet → then close 22. Web console as fallback.
- Lost restic password → stored in password manager; drill proves the stored copy works.
- Docker group = root-equivalent → only root uses Docker on this box.

## Dependencies
- Depends on K1 (shared server), ADR-006 (Postgres), ADR-008 (backups as audit layer 3).
- Constrains Phase 2 (gateway binds to the Tailscale address; desktop reaches it over the tailnet) and Phase 3 (vault trigger).
- Needs from Manj: Tailscale account sign-in on server and PC; R2 (or B2) bucket; password manager entries; Hostinger account 2FA.
