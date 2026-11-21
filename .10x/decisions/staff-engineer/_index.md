# Staff Engineer — index
## Active features
- news-wall
- phase-0-foundations (Python server standards in its file)
## Standards (all features)
- Rust stable, edition 2021; `cargo fmt`, `cargo clippy -D warnings` in CI.
- Async: tokio. HTTP: reqwest (rustls). Errors: `thiserror` in libs, `anyhow` in the app binary. Logging: `tracing`.
- Tests first (TDD); every trait has a fake; network tests use recorded fixtures, never the live internet.
- No secrets in repo; `.gitignore` covers models/ and any local config.
- CI on GitHub Actions: ubuntu + windows runners.
