# Architect — index
## Active features
- news-wall — slice 1 architecture (ADR-001..004)
## Principles
- Rust owns all state and logic; the webview only renders `ScreenState`.
- Parts communicate by typed messages over channels (tokio), not shared mutable state.
- Every external dependency (mic, model, API, network) sits behind a trait with a fake for tests.
