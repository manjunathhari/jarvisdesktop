# CTO — index
## Active features
- phase-1-memory-engine — v2 memory engine (ADR-010, approved 2026-11-22)
- jarvis-shell — desktop shell framework choice (proposed, awaiting user approval)
- news-wall — slice 1 strategy (accepted 2026-11-21)
- phase-0-foundations — v2 server foundations (proposed 2026-11-21)

## Cross-cutting principles
- Local-first: everything on the PC except the brain; brain swappable toward local open-source models.
- Windows first; OS-specific code isolated in one `platform` crate.
- Rust core; web layer only for drawing panels.
- Light: no frameworks or dependencies we don't need (YAGNI).
