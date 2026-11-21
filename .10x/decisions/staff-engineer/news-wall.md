# Staff Engineer — news-wall

## Cross-cutting
- **Config:** `config/sources.toml` (feeds, channels), `jarvis.toml` in user config dir (model ids, thresholds). Defaults compiled in.
- **Secrets:** Anthropic key via `keyring` crate → Windows Credential Manager. First-run: JARVIS asks the user to paste the key once into a one-off setup prompt (CLI `jarvis setup-key`), never stored in files.
- **Logging:** `tracing` to a rolling local log file; transcripts logged only at debug level and off by default.
- **Models:** downloaded to `%LOCALAPPDATA%/jarvis/models` on first run with checksum verification; never committed.
- **Latency telemetry:** coordinator records timestamps per stage (speech end → transcript → first brain event → panel shown) into the log, so the 3 s target is measured, not guessed.
- **Reuse:** feed parsing via `feed-rs`; HTML stripping of summaries via `ammonia` (sanitise before display).
- **Security:** UI CSP: `default-src 'self'; frame-src https://www.youtube.com https://www.youtube-nocookie.com; img-src 'self' https: data:`. Tauri capabilities: UI can only invoke `tap` and receive `screen://state`.
