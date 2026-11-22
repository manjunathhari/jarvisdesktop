# Status — JARVIS Desktop

**Phase:** Desktop node in tweak mode (merged to main 2026-11-21, PR #1). **Architecture v2 Phase 0 (server): complete, PR #2 merged 2026-11-21; running on the shared server. Phase 1 (memory engine): spec approved 2026-11-22, implementation in progress on branch `phase-1-memory-engine`.**
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
- [ ] Milestone B: [x] T9 ears/STT (tap-to-talk; awaiting on-device check) · [ ] T10 wake word · [x] T11 voice (George, Kokoro) · [x] T12 model download (voice) — verified on the user's PC 2026-11-21 (sounds good, speed fine, latency acceptable)
- [ ] Milestone C: T13 Windows platform · T14 end-to-end + latency · T15 QA/security + installer

## 2026-11-21 evening — handover to Architecture v2 Phase 0
Desktop node is now in "small tweaks as we go" mode (user decision). Latest installer: build 10.
Verified on device: news wall, briefing, Al Jazeera live, George voice, tap-to-talk listening, own window, exit.

### Desktop backlog (tweaks)
1. Voice barge-in: Windows Voice Clarity via Communications Signal Processing Mode (raw WASAPI capture with
   AudioCategory_Communications); fallback pure-Rust AEC3 (`sonora-aec3`) with George's audio as reference. Needs its own ADR (next free number, 010+; 006–009 are the server).
2. "Hey Jarvis" wake word (sherpa-onnx KWS; keyword file needs BPE tokenisation).
3. Global hotkey to summon/talk (e.g. Ctrl+Shift+J); bring window to front on wake.
4. Latency: read the new timing logs; swap Whisper small.en → Moonshine base if transcription dominates.
5. Verify feed URLs; orb pulses with George; start at login; auto-update (Tauri updater + signing key).
6. Later: desktop Brain becomes a client of the v2 server core (Phase 2).

## 2026-11-21 — Architecture v2 Phase 0 (server foundations) started
Kick-off decisions (Manj):
- K1 Server: existing Hostinger KVM 2, Mumbai, **shared** (pubmedgemini, Ollama, Traefik, standalone Postgres 16 stay). gemma-lab projects deleted from it.
- K2 Repo: this repo, server under `server/`.
- K3 Database: dedicated Postgres 17 container for JARVIS; existing Postgres 16 untouched.
- K5 Recovery: losing data OK, losing JARVIS not — rebuild from repo; shared server temporary, move out once functioning; local backups only for now.
- K4 Stack: Python 3.12, uv, psycopg 3 + plain SQL, Alembic, pytest, ruff, mypy; FastAPI from Phase 2.
- `milestone-a` merged into `main` (PR #1).

Design: `.10x/specs/2026-11-21-phase-0-foundations-design.md`, ADR-006…009 (Proposed).

### Needs Manj
- Review spec + ADR-006…009.
- ~~Backup storage~~ — decided: data loss acceptable; local daily backups + Hostinger weekly; JARVIS rebuildable from repo. Shared server is temporary; JARVIS moves out once functioning (K5).
- Repo is public: confirm OK for server code (no secrets/addresses will be committed), or make it private.
- Later, at hardening: Tailscale sign-in on server and PC; Hostinger 2FA.

### Phase 0 task list (after approval)
- [x] T0.1 CI path filters + `server/` skeleton + server.yml
- [x] T0.2 Contracts (JSON Schema + pydantic + examples + tests)
- [x] T0.3 Migrations: roles, ledger, audit, ops, allowed_transition, transition(); P0-1…P0-6 green in CI
- [x] T0.4 Job queue + scheduler heartbeat
- [x] T0.5 Hardening: Tailscale (+ Tailscale SSH), SSH key-only + fail2ban, Hostinger firewall 22/80/443 — done 21 Nov. **P0-8 PASS** 21 Nov (external scan: only 22/80/443 open; SSH publickey only). Re-runs weekly.
- [x] T0.6 Deployed via Hostinger Docker manager (image from GHCR); P0-1…P0-5 SMOKE PASS on the live server, 21 Nov
- [x] T0.7 Local daily backups running; rebuild/move runbook `server/deploy/README.md`. P0-7 restore drill deferred by Manj (Hostinger weekly backup suffices until the build grows).
- [x] T0.8 QA + security review (`.10x/reviews/2026-11-21-*`), no blocking issues — PR #2 merged 2026-11-21

## 2026-11-22 — Architecture v2 Phase 1 (memory engine)
Spec `.10x/specs/2026-11-21-phase-1-memory-engine-design.md` approved by Manj; ADR-010 accepted.
Decisions: P1 engine only (no real memories until Phase 2) · P2 markdown is the truth · P3 private git repo, no remote · P4 deterministic retrieval · P5 audit in Postgres, no content · P6 viewer-based access · no automatic parental access to PRIVATE · dedicated server before real memories (encryption at rest later).
Boundary: no embeddings, AI inference, consolidation, external memory services or real personal data in Phase 1.

### Phase 1 task list
- [x] T1.1 ADR-010; interface, types, memory-entry contract
- [x] T1.2 Strict markdown parser and writer
- [x] T1.3 Private git store and history
- [x] T1.4 Lifecycle and tag rules
- [x] T1.5 Visibility, retrieval, M1–M4
- [x] T1.6 Migration 0007 (memory audit + S5) and recovery
- [x] T1.7 Deploy: git in image, memory volume, smoke, bundle, runbook
- [ ] T1.8 Independent QA and security review (in progress)

Build: 217 tests green locally (Postgres 16); ruff, format, mypy clean. Deviations from the spec: `decisions/sde/phase-1-memory-engine.md`.
