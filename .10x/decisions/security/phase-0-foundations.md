# Security — phase-0-foundations
**Status:** Proposed (2026-11-21), ADR-009
- Threats: internet-facing neighbours (Traefik, pubmedgemini) on the same VM; SSH exposure; secret leakage via public repo or env vars; audit tampering; backup theft.
- Controls: Hostinger firewall 22/80/443 (+UDP 41641) only; SSH key-only + fail2ban (public for pubmedgemini's deploy, which is left untouched); JARVIS publishes no ports; compose file secrets in /etc/jarvis/secrets (0600); audit append-only by grants + trigger; local daily backups (off-site deferred to the move); Hostinger 2FA; web console break-glass.
- Accepted residual risk: shared host; internet-facing key-only SSH kept for pubmedgemini's deploy until JARVIS moves out.
