# Status — JARVIS Desktop

**Phase:** 4 — Implementation: Milestone A complete (branch `milestone-a`); next Milestone B
**Started:** 2026-11-21

## Known so far
- Vision: blank full-screen desktop driven entirely by voice ("Hey Jarvis").
- First slice: tap + "Hey Jarvis" → spoken greeting → "bring me the latest news" → multi-panel news wall (headlines/snippets + live news video).
- Stack preference from user: Rust (proposed: Tauri 2 from day one, see decisions/cto/jarvis-shell.md; pending approval).
- Target OS: **Windows** (answered 2026-11-21).
- News wall default: **Mix** — Indian headlines + Indian live TV as main panel, international sources alongside (answered 2026-11-21).

- Brain: **Claude API first**, built as a swappable Brain interface so it's hybrid-ready from day one (local open-source models for simple commands/offline later). User has an Anthropic API key (answered 2026-11-21).

- Design section 1 APPROVED: Approach 1 (all local except brain) + five-part shape Ears/Brain/Voice/Modules/Screen. Ears = openWakeWord + whisper.cpp (both open source).

- Design section 2 APPROVED: tap/wake → greeting → listen → Whisper → Claude tool menu → module → numbered panel wall → spoken reply + ~10 s follow-up window; failure handling as specced; 2–3 s target.
- Voice: defaulted to A — local open-source natural voice (user did not pick; confirm in spec review).
- Repo: github.com/manjunathhari/jarvisdesktop (was empty; state moved here 2026-11-21).

- Design section 3 APPROVED (testing + security).

- Spec APPROVED by user 2026-11-21 (voice default A accepted). Push to GitHub approved.

## Target PC (reported 2026-11-21)
- Windows 11 Home 25H2 (build 26200), i7-13620H 10C/16T, 23.7 GB RAM, 122 GB free
- GPU: Intel UHD only (no NVIDIA driver found) → Whisper runs on CPU; start with small English model, measure latency in T9
- Mic: Realtek microphone array; WebView2 157 present
- Dev tools not yet installed (Git, Node, Rust, VS C++ Build Tools) — winget steps given

## Needs the user
- Milestone A ran on the Windows PC (2026-11-21). Feedback: voice is robotic; needed exit; terminal annoying → installer + key setup shipped (build 1).
- Decide next: Milestone B (voice first) vs Architecture v2 Phase 0/1 (see project docs).
- Approve web lookups so feed URLs / channel IDs can be verified.

## Open questions


## Task list (news-wall) — see decisions/engineering-manager/news-wall.md
- [x] Milestone A: T1 scaffold+CI · T2 core types · T3 coordinator · T4 news · T5 live_tv · T6 ClaudeBrain · T7 brain evals · T8 Tauri UI (81 tests green)
- [ ] Milestone B: T9 ears/STT · T10 wake word · T11 TTS spike+voice · T12 model downloads
- [ ] Milestone C: T13 Windows platform · T14 end-to-end + latency · T15 QA/security + installer
