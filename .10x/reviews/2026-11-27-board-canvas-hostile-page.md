# Board Canvas seal: the hostile-page check

Date: 27 November 2026. Board slice 1, Task 8.

A Canvas panel is a web page the model writes, partly from what it read on the web. It must be able to draw and nothing else. This is the check that it is sealed.

## A change from the plan, and why

The plan served Canvas pages from a URI scheme registered with the app (`jarvis-canvas`). Reading Tauri 2.12.7 and wry 0.57.3 showed that would not seal the page:

- `Webview::is_local_url` (tauri `src/webview/mod.rs`) counts a page from any scheme the app registered as local, with the app's own right to call commands.
- On Windows, start-up scripts are added to every frame whatever `for_main_frame_only` says (wry `src/lib.rs`), so the command interface and its key are present inside a sandboxed frame.
- wry's message handler passes the sending frame's own address (`args.Source()`), so a call from that frame would be judged local and allowed.

So the page is served from a small server on the loopback instead (`crates/jarvis-node/src/canvas.rs`). `http://127.0.0.1:<port>` is not local to Tauri, and this Tauri version refuses every command from a non-local page that no capability names. Manj approved the change on 27 Nov 2026.

## What seals the page

1. **Not the app's origin.** Commands from it are refused by Tauri.
2. **The frame's sandbox:** `sandbox="allow-scripts"` only. No same-origin (so no storage, cookies or reading its own server), no forms, pop-ups, top navigation or downloads.
3. **The page's own policy**, sent with every answer: `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'; sandbox allow-scripts`. No request of any kind can leave.
4. **The Board's policy** allows frames only from `http://127.0.0.1:47821`, `:47822`, `:47823`, so the page cannot point its own frame elsewhere.

What protects panels from other programs on the PC: loopback only; a secret in every address, new each run; `Host` must be `127.0.0.1:<port>`; only `GET` of one exact path, for a canvas panel at the revision held.

## The test page

`crates/jarvis-node/examples/hostile.html` (fictional; it attacks only its own sandbox). It tries 27 things and shows one line for each, `CONTAINED` or `ESCAPED`, then a verdict. Two buttons: "Start an endless loop" and "Try to leave this page".

It tries to: read the Board's document, the top window's address and a function of the Board; use localStorage, sessionStorage, cookies and indexedDB; open a window; submit a form; fetch an outside address, its own server and the app's command channel; load an outside image, script and stylesheet; send a beacon; open a WebSocket and an event stream; frame an outside page; start a worker from outside; call six app commands (`board_snapshot`, `board_clear_closed`, `quit`, `say`, `plugin:event|listen`, `plugin:opener|open_url`); post straight to the app's message channel.

## Result in Chrome (27 Nov 2026, Linux, headless Chrome)

The page was served by the real Canvas server (`cargo run -p jarvis-node --example canvas-hostile -- serve`) into a frame with the Board's sandbox attribute, inside a stand-in page carrying the app's content policy.

- All 27 attempts contained.
- Requests that really left the page, counted outside the browser's policy: none.
- "Try to leave this page": the frame went to an error page, not the outside address.
- "Start an endless loop": the stand-in page still answered clicks.

**Not covered by this run:** the six app-command attempts and the message channel. Chrome has no Tauri, so they reported "no interface". On Windows the interface will be present in the frame, and the calls must be refused. That is the point of the Windows run.

## To run on Windows (with Manj's go-ahead; part of the Task 9 live check)

1. Close JARVIS. In the app's data folder, in `node`, copy `board.json` somewhere safe.
2. Put the prepared `board.json` there (made with `cargo run -p jarvis-node --example canvas-hostile -- write <folder>`; Claude supplies the file).
3. Start JARVIS and open the Board (F9). The panel "Canvas seal check (fictional)" is restored and runs.
4. Pass: the verdict is "All 27 attempts contained." The six "call the app" lines must say CONTAINED with a refusal, whether or not they say the interface is present. JARVIS is still running (the `quit` attempts did nothing) and said nothing aloud (the `say` attempt did nothing).
5. Click "Try to leave this page": the panel shows an error or goes blank; the Board itself is unchanged.
6. Click "Start an endless loop": the other panels, the header buttons and the conversation column still respond; "Close panel" on this panel ends it.
7. Close JARVIS and put the saved `board.json` back.

If any line says ESCAPED, or the loop freezes the Board: stop, do not ship Canvas, and report. The fallback is a separate window for each Canvas panel.

## Result on Windows

Not yet run.
