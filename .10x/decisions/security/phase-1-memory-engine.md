# Security — phase-1-memory-engine
- Audit trail is append-only, so nothing free-form may enter it: reason codes only (SQL-enforced), actor shape checked, legal tag changes only.
- Memory repo: no remote, no extra local git config, no attributes, hooks and fsmonitor off.
- Visibility per viewer; no automatic parental access; hidden = missing on every call.
- Gate: no real memories until JARVIS is on its own server; encryption at rest after.
- Review: `.10x/reviews/2026-11-22-phase-1-security-review.md`.
