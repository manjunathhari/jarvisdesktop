# Security — phase-0-foundations
**Status:** Proposed (2026-11-21), ADR-009
- Threats: internet-facing neighbours (Traefik, pubmedgemini) on the same VM; SSH exposure; secret leakage via public repo or env vars; audit tampering; backup theft.
- Controls: Hostinger firewall 22/80/443 (+UDP 41641) only; SSH key-only + fail2ban (public for pubmedgemini's deploy, which is left untouched); JARVIS publishes no ports; compose file secrets in /etc/jarvis/secrets (0600); audit append-only by grants + trigger; local daily backups (off-site deferred to the move); Hostinger 2FA; web console break-glass.
- Accepted residual risk: shared host; internet-facing key-only SSH kept for pubmedgemini's deploy until JARVIS moves out.
- Review 2026-11-21: no blocking issues; accepted S1 (public key-only SSH for pubmedgemini), S2 (shared host); follow-ups S3 (gateway must derive actor identity), S4 (pin image digests before the move), S5 (revoke CONNECT on db `postgres`). See `.10x/reviews/2026-11-21-phase-0-security-review.md`.
