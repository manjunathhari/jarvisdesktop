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

The status line at the bottom right shows the link. In server mode the Anthropic key is never read. Once you have accepted Phase 2, remove it: in Windows **Credential Manager → Windows Credentials**, delete the entry "JARVIS Desktop" (the app's `forget_claude_key` command does the same). Delete `node.toml` to go back to the desktop's own brain.

Deletes only ever go to the Recycle Bin, only from `test_folder`, and only after you approve the card (tap, or say "approve" when one card is showing).

## The Board

In server mode JARVIS has a second window, the Board, for anything worth looking at: an answer written out, a comparison, a chart. Ask for something to be shown, looked up, compared or drawn. Jarvis answers at once, does the work on the server, puts a panel on the Board, and then says one sentence about it.

- **Where it opens.** With two monitors, full screen on the one JARVIS is not on, when the app starts. With one monitor, as an ordinary window when a panel arrives. **F9** in the JARVIS window opens it at any time. Closing the Board only hides it.
- **Panels.** A *text* panel is formatted writing; a *canvas* panel is a small page Jarvis draws. Each has Minimise, Maximise and Close, by click or by voice ("minimise the chart", "bring back the comparison"). Minimised panels wait in the dock; closed ones are listed under Recently closed. Nothing is deleted on the server.
- **Conversation.** The left column shows what you said and what Jarvis said, with a button for each panel a reply brought. You can type there instead of speaking, except to decide an approval: that is done on the card in the JARVIS window. If Jarvis is busy or not listening, the box says so and keeps your text.
- **What is kept on this PC.** The conversation (every line heard, typed and spoken, as plain text) and the panels are saved in the app's data folder, in `node\conversation.jsonl` and `node\board.json`, so they survive a restart. The conversation file is never trimmed. To clear it, close JARVIS and delete the file.
- **Spend.** The top bar shows what today's panels cost. Every Board request counts against the monthly cap.
- **After a restart** the panels and the conversation are back as they were.

What Jarvis puts on the Board is treated as untrusted, because it is partly written from web pages:

- In a text panel, HTML is shown as text, no image is loaded, and only a plain `https` link is a link. It shows its real address, and a click opens your browser after the app has checked the address again.
- A canvas page is served from a small server on this PC only and shown in a sealed frame. It cannot reach the network, the app, your files or the other panels, and the Board window is muted. `.10x/reviews/2026-11-27-board-canvas-hostile-page.md` describes the seal and how it is checked.
- Each window can call only its own commands of the app. The Board has none that quits the app, decides an approval, enrols a device or touches the API key.

Tests for the page: `cd app && npx vitest run`.

The Rust node's end-to-end tests run against the real server code:

```bash
cargo build -p jarvis-node --features test-support --bin jarvis-node-probe
cd server && JARVIS_NODE_PROBE=../target/debug/jarvis-node-probe uv run pytest tests/test_rust_node.py
```
