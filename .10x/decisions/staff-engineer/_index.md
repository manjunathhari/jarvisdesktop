# Staff Engineer — index
## Active features
- phase-1-memory-engine — v2 memory engine (ADR-010, approved 2026-11-22)
- news-wall
- phase-0-foundations (Python server standards in its file)
## Standards (all features)
- Rust stable, edition 2021; `cargo fmt`, `cargo clippy -D warnings` in CI.
- Async: tokio. HTTP: reqwest (rustls). Errors: `thiserror` in libs, `anyhow` in the app binary. Logging: `tracing`.
- Tests first (TDD); every trait has a fake; network tests use recorded fixtures, never the live internet.
- No secrets in repo; `.gitignore` covers models/ and any local config.
- CI on GitHub Actions: ubuntu + windows runners.
