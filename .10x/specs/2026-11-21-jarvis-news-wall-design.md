# JARVIS Desktop — Slice 1 Design: Voice Wake + News Wall

**Status:** Awaiting user review
**Date:** 2026-11-21
**Feature slug:** `news-wall`
**Repo:** github.com/manjunathhari/jarvisdesktop

---

## 1. Purpose

A voice-only desktop in the spirit of JARVIS from *Iron Man*: a black, empty full-screen display with no icons, taskbar or buttons. The user taps the screen or says "Hey Jarvis", JARVIS greets him by voice, and on request it puts up a wall of news panels that he can keep talking to.

This first slice proves the whole loop end to end (wake → listen → understand → act → show → speak) with one capability: **news**. Every later capability (calendar, Gmail, social media, PC control) plugs into the same loop as a new module.

### Success criteria

1. Tapping the screen or saying "Hey Jarvis" wakes JARVIS and it greets the user aloud.
2. Saying "bring me the latest news", or any reasonable rewording, shows the news wall described in §4.
3. Follow-ups work by voice: "open number 4", "tell me more about number 2", "switch to another channel", "close the video", "clear the screen".
4. Under 3 seconds from the end of the user's sentence to the panels appearing, on the target PC with a normal connection.
5. Raw microphone audio never leaves the PC.

### Out of scope for slice 1

Calendar, Gmail, social media, control of other Windows apps, a premium cloud voice, local brain models (the interface is ready for them, but they are not built), and replacing the Windows shell at boot.

---

## 2. Decisions taken in brainstorming

| Topic | Decision |
|---|---|
| Target OS | **Windows** first. All OS-specific code is kept in one `platform` module so a Linux or macOS version needs only that module rewritten. |
| Shell framework | **Tauri 2**: a Rust core with a thin WebView2 window for panels. Starting here, with no planned move to Electron. See `decisions/cto/jarvis-shell.md`. |
| Overall approach | **Approach 1**: everything runs on the PC except the brain. |
| Wake word | **openWakeWord** (Apache-2.0), using its pretrained "hey jarvis" model, run locally. Tapping the screen also wakes JARVIS. |
| Speech to text | **whisper.cpp** (MIT), run locally. Model size is chosen by speed on the target PC. |
| Brain | **Claude API** first, behind a swappable `Brain` interface. It is designed so a hybrid can come later: a local open-source model for simple commands and offline use, Claude for open-ended requests. |
| Voice (TTS) | **Default: a natural open-source voice run locally.** *The user has not confirmed this; please confirm in review.* It sits behind a swappable `Voice` interface, so a premium cloud voice can be added later. |
| News default | **Mix:** Indian headlines and Indian live TV as the main panel, international sources alongside. |

---

## 3. Architecture

Five parts, each with one job, connected by simple messages. Each can be built and tested on its own.

```
 Microphone
    │
 [Ears]    wake word + speech-to-text                       (Rust, on PC)
    │  text
 [Brain]   Claude now / local model later → reply + action   (swappable)
    │
 ┌──┴──────────────┐
[Voice]          [Modules]   news · live-tv  (later: calendar, gmail, …)
speaks reply      fetch data in Rust → panel content
                     │
               [Screen]   black full-screen Tauri window with panels
```

| Part | Does | Depends on |
|---|---|---|
| **Ears** | Listens for the wake word. After waking, records until the user stops talking (detected automatically) and turns the speech into text. | Microphone, openWakeWord, whisper.cpp |
| **Brain** | Takes the transcribed text plus what is currently on screen. Returns a short spoken reply and at most a few actions, chosen only from the actions modules have registered. | Claude API (now) |
| **Voice** | Speaks a reply. Stops immediately if the user starts talking or says "stop". | Local TTS engine |
| **Modules** | Each module registers (a) the actions it offers the Brain, (b) a fetcher that gets its data, and (c) the panel type it renders. Slice 1 has two: `news` and `live-tv`. | Internet (news feeds, YouTube) |
| **Screen** | Draws the black canvas, the status orb, the caption line and the panels. It has no buttons and displays only what Modules send it. | Tauri / WebView2 |
| **Coordinator** | The loop that joins the parts: idle → awake → listening → thinking → acting/speaking → follow-up window → idle. It also tracks what is on screen so the Brain knows what "number 4" refers to. | All of the above |

### Module contract (how future features plug in)

Every module provides three things:

1. **Actions:** a name, a one-line description and parameters, e.g. `show_news(region?, topic?)`. These become the tools Claude can call.
2. **Fetcher:** Rust code that gets the data, with caching and timeouts.
3. **Panel:** the type of card or player the Screen draws for it.

Adding Gmail later means adding one module that follows this contract. Nothing else changes.

### Slice 1 action menu

| Action | Module | Effect |
|---|---|---|
| `show_news(region?, topic?)` | news | Shows or refreshes the news wall |
| `open_story(number)` | news | Expands a story card with its fuller summary |
| `summarise_story(number)` | news | JARVIS reads a short spoken summary |
| `play_live_tv(channel?)` | live-tv | Plays a channel's live stream in the video panel |
| `next_channel()` | live-tv | Switches to the next channel on the list |
| `close_panel(which)` | screen | Removes a panel |
| `clear_screen()` | screen | Returns to the empty black screen |

---

## 4. Flow: from tap to news wall

1. **Waiting.** A black screen with a small, softly glowing orb. The microphone is analysed on the PC only for the wake word; nothing is recorded, saved or sent.
2. **Waking.** A tap or "Hey Jarvis" makes the orb brighten with a short chime, and JARVIS says, e.g., *"Good morning, Manj. How can I help?"* The greeting is built locally from the time of day, so it is instant.
3. **Listening.** JARVIS records until the user pauses, then shows what it heard as a faint caption at the bottom.
4. **Understanding.** whisper.cpp turns the speech into text, and the text plus a description of what is on screen goes to the Brain.
5. **Doing.** The Brain picks an action. The news module serves headlines from a cache refreshed in the background every few minutes, so it does not fetch while the user waits. The live-tv module looks up the chosen channel's YouTube live stream.
6. **Showing.**

```
┌───────────────────────────────┬──────────────────┐
│  LIVE: Indian news channel    │  INTERNATIONAL   │
│  (video)                      │  ① BBC …         │
├───────────────────────────────┤  ② Reuters …     │
│  INDIA                        │  ③ Al Jazeera …  │
│  ④ The Hindu …  ⑤ NDTV …      │                  │
│  ⑥ India Today …              │                  │
└───────────────────────────────┴──────────────────┘
```

   Every story carries a number that can be used by voice.
7. **Speaking.** JARVIS reads the top story's headline, then keeps listening for about 10 seconds so the user can follow up without saying "Hey Jarvis" again. After that it returns to waiting, and the panels stay on screen until cleared.

### News sources (initial list, editable in one config file)

- **India (main panel):** The Hindu, NDTV, India Today, Times of India, via their public RSS feeds.
- **International (side column):** BBC, Reuters, Al Jazeera, via public RSS feeds.
- **Live TV:** a short ordered list of Indian news channels' official YouTube live streams, with international channels as fallbacks.

Exact feed addresses are confirmed during implementation, because they change over time.

### Error handling

| Situation | Behaviour |
|---|---|
| No internet | JARVIS says so and shows the last cached headlines, marked with their age. |
| Speech not understood or empty | "Sorry, I didn't catch that." Stays in the listening state once, then goes back to waiting. |
| Brain request fails or times out (about 8 s) | Spoken apology; fixed commands ("clear the screen", "stop") still work locally. |
| One news feed is down | That source is left out silently. |
| Live stream offline | Moves to the next channel on the list and says which one is playing. |
| Wakes by mistake | No speech is heard within a few seconds, so it returns to waiting silently. |

### Performance target

Under 3 seconds from the end of the user's sentence to the panels appearing. The main costs are speech-to-text and the Brain round trip, so: headlines are pre-cached, the Brain's reply is streamed so the action can start before the spoken text is finished, and the Whisper model size is chosen for speed.

---

## 5. Testing strategy

- **Ears:** a library of recorded clips: "Hey Jarvis" and commands in the user's voice and others, with background TV, a fan and conversation. It measures both missed wakes and false wakes, and checks transcription of Indian English.
- **Brain:** a fixed evaluation set of about 30 phrasings per action, each with the expected action and parameters, run against Claude. The same set is the yardstick for comparing local models later.
- **Modules:** saved copies of real feeds, including a broken one and an offline stream, so tests do not need the internet.
- **Coordinator:** state-machine tests for every transition, including interruptions ("stop" while speaking) and follow-up timeouts.
- **End to end on Windows:** timed runs of the full loop against the 3-second target.

Code is written test-first.

---

## 6. Security and privacy

- **Raw audio never leaves the PC.** Only the text of what was said *after* waking goes to the Brain.
- **A visible listening indicator.** The orb glows whenever audio is being recorded. "Jarvis, stop listening" turns the microphone off until the screen is tapped.
- **The API key is stored in Windows Credential Manager.** It never goes in source code, config files or the GitHub repo.
- **The Brain can only call registered actions.** It cannot run programs, access files or send messages. News text is outside content and may contain injected instructions, so in slice 1 the worst outcome is the wrong panel being shown. Every future module that can change things (send an email, post) must ask for spoken confirmation before acting; that rule is set now.
- **Panels are display-only.** Embedded video plays in a sandbox, and no third-party page can call into the Rust core.

---

## 7. Later slices (for context only, not designed here)

1. Always-on wake without a tap, and auto-start at login. Full takeover of the Windows desktop.
2. Calendar and Gmail modules through Google's official APIs.
3. Hybrid brain: a local open-source model for fixed commands and offline use.
4. Social media, once what is realistically possible is scoped (Facebook does not offer a personal-feed API; X's API is paid).
5. A Linux version through the `platform` module, possibly on a dedicated mini-PC.
