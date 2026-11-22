# Architect — index
## Active features
- phase-1-memory-engine — v2 memory engine (ADR-010, approved 2026-11-22)
- news-wall — slice 1 architecture (ADR-001..004)
- phase-0-foundations — v2 server foundations (ADR-006..009, proposed)
## Principles
- Rust owns all state and logic; the webview only renders `ScreenState`.
- Parts communicate by typed messages over channels (tokio), not shared mutable state.
- Every external dependency (mic, model, API, network) sits behind a trait with a fake for tests.
