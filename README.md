# JARVIS Desktop

A voice-only desktop for Windows: a black full-screen display you talk to. Say "Hey Jarvis", ask for the news, and it builds a wall of live TV and headlines you can keep talking to.

**Status:** design and planning complete; implementation starting (slice 1: voice wake + news wall).

- Design spec: `.10x/specs/2026-11-21-jarvis-news-wall-design.md`
- Architecture decisions: `.10x/adrs/`
- Progress: `.10x/status.md`

Stack: Rust + Tauri 2 (WebView2), openWakeWord, whisper.cpp, local TTS, Claude API (swappable for local models).
