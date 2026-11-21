# ADR-001: Tauri 2 as the desktop shell

**Status:** Accepted
**Date:** 2026-11-21
**Feature:** news-wall
**Author:** 10x-Team (Architect + Staff Engineer)

## Context
JARVIS must be a black, borderless, always-on full-screen window on Windows that can show live video and rich panels, stay light (always running), grow to many modules, and move to Linux later. User prefers Rust.

## Decision
Use **Tauri 2**: Rust core (all logic) + one WebView2 window used only to draw panels. Frontend is plain TypeScript/HTML/CSS built with Vite, with no UI framework.

## Alternatives Considered
| Alternative | Pros | Cons | Why Not |
|---|---|---|---|
| Electron | Bundled Chromium everywhere; mature tooling | ~10x larger, several times more idle RAM; JS backend | On Windows WebView2 is already Chromium, so its main advantage vanishes |
| Pure-Rust UI (egui/iced/Slint) | Lightest, no web layer | No good way to play YouTube live or rich web content | Fails the live-video requirement |
| Browser in kiosk mode | Fastest prototype | No real access to mic pipeline, OS, credentials | Throwaway |

## Consequences
### Positive
- Small installer, low RAM, fast start; Rust everywhere that matters.
- Same codebase builds for Linux/macOS (and mobile) later.
### Negative
- Multiple webviews in one window need Tauri's `unstable` flag → avoided by design (ADR-003: modules render our own cards, not embedded websites).
- Linux uses WebKitGTK → panels stay on widely supported web features.
### Risks
- Frontend must not become a second application. Mitigation: frontend only renders `ScreenState` sent from Rust; no business logic in TS.

## Dependencies
Constrains: ADR-003 (panels are data-driven cards), platform crate design.
