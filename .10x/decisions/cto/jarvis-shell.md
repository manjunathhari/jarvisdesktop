# CTO — jarvis-shell (PROPOSED, 2026-11-21)

## Question from user
Tauri vs Electron for a shell that will grow to host news, live video, calendar, Gmail, social media — must stay light, fast, scalable; avoid a later migration.

## Recommendation: Tauri 2 from day one, no planned migration
- Target is Windows only → Tauri renders with WebView2, which is Chromium. Electron's main advantage (identical Chromium everywhere) mostly disappears on Windows.
- Much lower installer size, idle RAM, startup time vs Electron — matters for an always-on shell.
- Rust core matches user's stack preference and suits the always-on audio loop (wake word, local STT).

## Known risk
- Several live third-party *websites* side by side in one window needs Tauri child webviews, which are behind the `unstable` Cargo flag.
- Mitigation: integrations come in through APIs and render as our own lightweight cards (not embedded websites). Video uses YouTube embeds. Separate webview windows as fallback.

## Scalability principle
Scale comes from the module contract, not the shell: each integration = (1) tools the brain can call, (2) a data fetcher in Rust, (3) a panel renderer. Shell stays thin and swappable.

## Caveats flagged to user
- Facebook API does not expose a personal news feed; X API is paid. Social integrations may need to be scoped differently.

## Portability (user asked 2026-11-21: what if we move to Linux/other devices?)
- Tauri targets Windows, macOS, Linux (and Android/iOS in v2) from one codebase.
- Linux uses WebKitGTK, not Chromium → test video embeds/CSS there; keep panels on plain, well-supported web features.
- Design rule: all OS-specific behaviour (full-screen takeover, autostart, mic permissions, later app control) lives behind one Rust `platform` module with a Windows impl first. Audio (cpal) and whisper.cpp are already cross-platform.
- Linux is actually the cleanest "blank desktop": boot straight into JARVIS as the only app (kiosk session).
