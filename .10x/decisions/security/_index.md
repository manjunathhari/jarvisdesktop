# Security — index
## Active features
- phase-2-core-and-nodes — reviewed 2026-11-24; S-1 fixed (R12-34); live egress proof pending deploy
- phase-1-memory-engine — reviewed 2026-11-22, no blocking issues
- phase-0-foundations — server hardening, secrets, backups (ADR-009, proposed)
## Principles
- Tailscale-only access to anything Jarvis; secrets never in the repo; append-only audit; encrypted off-site backups once JARVIS has its own server.
