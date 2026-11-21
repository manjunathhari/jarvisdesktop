# JARVIS Desktop

A voice-only desktop for Windows: a black full-screen display you talk to. Say "Hey Jarvis", ask for the news, and it builds a wall of live TV and headlines you can keep talking to.

**Status:** Milestone A done: brain, news, live TV and the screen work with typed input. Voice comes in Milestone B.

- Design spec: `.10x/specs/2026-11-21-jarvis-news-wall-design.md`
- Architecture decisions: `.10x/adrs/`
- Progress: `.10x/status.md`

Stack: Rust + Tauri 2 (WebView2), openWakeWord, whisper.cpp, local TTS, Claude API (swappable for local models).

## Try it on Windows (Milestone A)

You need, once: [Rust](https://rustup.rs), [Node.js 22+](https://nodejs.org), and the "Desktop development with C++" workload from Visual Studio Build Tools. WebView2 is already part of Windows 11.

```powershell
git clone https://github.com/manjunathhari/jarvisdesktop
cd jarvisdesktop\app
npm install
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # this window only; never saved to disk
npm run tauri dev
```

A black full-screen window opens. Tap the orb (JARVIS greets you), then type in the box, e.g. *bring me the latest news*, *open number 3*, *put on India Today*, *clear the screen*. Press Alt+F4 to quit.

Score the brain against the test phrasings:

```powershell
cargo run -p jarvis-brain --bin jarvis-eval
```
