# Handoff
**Now:** Architecture v2 Phase 0. Design approved (spec + ADR-006…009, K5 local backups only). T0.1–T0.4 built on branch `phase-0-foundations` (draft PR #2); CI green on Postgres 17 (70 tests, image builds).

**Next: T0.5 hardening** — see `decisions/sde/phase-0-foundations.md` for constraints:
- Build workspace cannot SSH to the server; Hostinger API cannot run host commands → write `server/deploy/harden/harden.sh` (idempotent) for Manj to paste into the Hostinger web console. It installs Tailscale (Manj approves the login URL), creates `/etc/jarvis/secrets` (root:10001 0440), sets SSH key-only, enables unattended upgrades.
- pubmedgemini deploy (repo manjunathhari/pubmedgemini, needs add_repo) must move onto the tailnet before the Hostinger firewall closes port 22.
- Then Hostinger firewall via the connector: allow TCP 80/443 + UDP 41641; verify P0-8.

**Then T0.6:** publish image to GHCR by digest; add one-shot `init` service; deploy compose via Hostinger Docker API; `smoke` = P0-1…P0-5 on server.
