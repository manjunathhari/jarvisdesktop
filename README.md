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

## Connecting the desktop to the Jarvis server

By default the desktop uses its own Claude brain, as above. To have the Jarvis server answer instead, with your memory, approvals and the test folder, create `node.toml` in the app's local data folder (`%LOCALAPPDATA%\in.mhsurgery.jarvis\`):

```toml
brain = "server"
gateway = "<your gateway's tailnet name>:<port>"   # host:port, never committed anywhere
test_folder = 'C:\Users\<you>\JarvisTest'          # optional: the only folder Jarvis may delete from
```

Then start JARVIS. It shows **Connect to the Jarvis server** the first time:

1. On the server, run `jarvis-core node enrol --person <you> --kind DESKTOP --name "<this PC>"`. It prints a one-time code.
2. Type the code into the screen and press **Connect**. The code works once and expires soon; it is never stored or logged.

The status line at the bottom right shows the link. In server mode the Anthropic key is never read; once you have accepted Phase 2, the `forget_claude_key` command removes it from Credential Manager. Delete `node.toml` to go back to the desktop's own brain.

Deletes only ever go to the Recycle Bin, only from `test_folder`, and only after you approve the card (tap, or say "approve" when one card is showing).

The Rust node's end-to-end tests run against the real server code:

```bash
cargo build -p jarvis-node --features test-support --bin jarvis-node-probe
cd server && JARVIS_NODE_PROBE=../target/debug/jarvis-node-probe uv run pytest tests/test_rust_node.py
```
