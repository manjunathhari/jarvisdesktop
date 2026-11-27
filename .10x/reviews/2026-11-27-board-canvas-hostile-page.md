# Board Canvas seal: the hostile-page check

Date: 27 November 2026. Board slice 1, Task 8.

A Canvas panel is a web page the model writes, partly from what it read on the web. It must be able to draw and nothing else. This is the check that it is sealed.

## A change from the plan, and why

The plan served Canvas pages from a URI scheme registered with the app (`jarvis-canvas`). Reading Tauri 2.12.7 and wry 0.57.3 showed that would not seal the page:

- `Webview::is_local_url` (tauri `src/webview/mod.rs`) counts a page from any scheme the app registered as local, with the app's own right to call commands.
- On Windows, start-up scripts are added to every frame whatever `for_main_frame_only` says (wry `src/lib.rs`), so the command interface and its key are present inside a sandboxed frame.
- wry's message handler passes the sending frame's own address (`args.Source()`), so a call from that frame would be judged local and allowed.

So the page is served from a small server on the loopback instead (`crates/jarvis-node/src/canvas.rs`). `http://127.0.0.1:<port>` is not local to Tauri, and this Tauri version refuses every command from a non-local page that no capability names. The owner approved the change on 27 Nov 2026.

## What seals the page

1. **Not the app's origin.** Commands from it are refused by Tauri (read in the sources; to be proved on Windows).
2. **The frame's sandbox:** `sandbox="allow-scripts"` only. No same-origin (so no storage, cookies or reading its own server), no forms, pop-ups, top navigation, downloads or dialogs.
3. **The page's own policy**, sent with every answer and demanded by the frame (`csp` attribute): `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'; sandbox allow-scripts`.
4. **The Board page's policy** (a `<meta>` in `board.html`) allows frames only from `http://127.0.0.1:47821`, `:47822`, `:47823`. The app-wide policy also allows YouTube, for the main window's news wall; the Board narrows it.
5. **The Board window's own browser** (its own data folder, so its own arguments; `canvas::BOARD_BROWSER_ARGS`): a proxy that is not there, with only `127.0.0.1` and `*.localhost` going round it, so no request reaches the network or the LAN; no WebRTC outside a proxy; no prerendering; no autoplay; and the whole window muted.
6. **A script that runs first in every frame of the Board window** (`canvas_seal.js`): removes WebRTC, the sound and speech interfaces, playing of media elements and the old clipboard command from Canvas pages and from frames they make.

Layers 4 to 6 were added after two independent reviews. The first judged the seal "not sound as committed"; the second, of the fixes, "sound after fixes" (muting, the link-local exception and the checklist below came from it). The first which showed that a page policy alone does not stop everything in Chrome 153:
- `<link rel="prerender">` sent a real request to any address, outside or on the loopback;
- WebRTC sent packets to any server (the `webrtc` policy directive is not recognised);
- a navigation the policy blocked still looked the name up and opened a connection;
- the frame could turn itself into a YouTube page, because the app-wide policy allows that for the main window;
- with the app's autoplay argument, a page could make sound without a click, and after one click it could play a generated tone whatever the policy.

What protects panels from other programs on the PC: loopback only; a secret in every address, new each run; `Host` must be `127.0.0.1:<port>`; only `GET` of one exact path, for a canvas panel at the revision held.

## The test page and the runner

`crates/jarvis-node/examples/hostile.html` (fictional; it attacks only its own sandbox). It shows one line per attempt:
- `CONTAINED` or `ESCAPED` where the page itself can tell;
- `CHECK` where it cannot (a request that fails quietly looks the same from inside as one that got out; a refused app command is answered in the Board's page, not in the frame). These are judged from outside.

Buttons: endless loop; leave the page; become a YouTube page; go to another local port; flood the app with messages.

`crates/jarvis-node/examples/canvas-hostile-check.py` runs the page from the real Canvas server in headless Chrome twice, as first committed and with the full seal, with listeners standing in for the outside world (every name resolves to the loopback), and prints what really arrived.

## Result in Chrome (27 Nov 2026, Linux, headless Chrome)

| | As first committed | With the full seal |
|---|---|---|
| Judged attempts | 13 escaped (WebRTC, sound and speech interfaces present, also in a child frame) | all 33 contained |
| Arrived outside, automatic attempts | a connection on port 80 (prerender) and STUN packets | nothing |
| "Try to leave this page" | blocked, but a connection was opened to the outside host | blocked, nothing arrived |
| "Try another local port" | the other program's page was shown in the panel | the request arrived there, its page was not shown |
| "Try to make a sound and copy text" (after a click) | copy, a generated tone and speech all went through | all refused |
| Endless loop, message flood | the stand-in page still answered | the same |

## What this does not show

- **Tauri.** Chrome has no Tauri: the six app-command attempts and the message channel are not exercised at all.
- **WebView2.** It is Chromium, but each of these must be seen on Windows: that the browser arguments are honoured; that the Board's own page and its command channel still work behind the dead proxy; that the all-frames script runs before a page's script in sandboxed and child frames; that the `csp` attribute and the `<meta>` policy behave as here.
- **Sound.** Whether a page can speak on Windows before the seal script removes the means was not heard.
- **Left as it is:**
  - a Canvas page can send `GET` requests, with paths it chooses and as often as it likes, to the two Canvas ports the app is not using (normally nothing listens there; a page answering there is not shown);
  - a navigation the Board blocks still opens an empty connection to another port on `127.0.0.1` (no data in it);
  - a page that knew another Canvas panel's random id could make its own frame load that panel;
  - a page written to use sound or speech will show an error instead (the authoring prompt does not yet say so);
  - if the Board window cannot be built, there is only a line in the log;
  - another program running as the same Windows user is not defended against.

## To run on Windows (with the owner's go-ahead; part of the Task 9 live check)

The release app has no visible log, so this run uses a build started from a console (Claude says how at the time), where the log is shown.

1. Close JARVIS. In the app's data folder, in `node`, copy `board.json` somewhere safe. Close a panel first so that "Recently closed" is not empty.
2. Put the prepared `board.json` there (made with `cargo run -p jarvis-node --example canvas-hostile -- write <folder>`; Claude supplies the file).
3. Start JARVIS and open the Board (F9). First: the Board itself appears and works. This proves its page and command channel work behind the dead proxy. If the Board is blank, stop here. The panel "Canvas seal check (fictional)" is restored and runs.
4. **The browser arguments are in force** (nothing else shows this, because other layers would hide a failure). Task Manager, Details tab, add the "Command line" column: among the `msedgewebview2.exe` rows, one group has a folder ending `board-webview\EBWebView` and carries `--proxy-server=http://127.0.0.1:1`, `--proxy-bypass-list`, `--webrtc-ip-handling-policy`, `NoStatePrefetchHoldback` and `--mute-audio`. The main window's group has a different folder and no proxy.
5. Judged lines: the verdict says all judged attempts were contained; no line says ESCAPED; the line "a frame made by this page is sealed too" says CONTAINED, not CHECK.
6. CHECK lines:
   - JARVIS is still running (the `quit` attempts did nothing), said nothing aloud, and no browser window opened;
   - the panels closed earlier are still under "Recently closed";
   - the console log has no "the Canvas server refused a request" line for `/probe-prerender`.
7. "Try to make a sound and copy text": the line beside it says every one was refused, nothing is heard, and pasting into Notepad gives whatever was on the clipboard before.
8. "Try to leave this page", "Try to become a YouTube page": the panel shows an error or goes blank; no YouTube page; the Board itself is unchanged.
9. "Try another local port", with a stand-in program answering on port 47823 (Claude supplies a one-line command): its page is **not** shown in the panel. This is the only check of the frame's demanded policy.
10. "Start an endless loop", then, on a fresh start, "Flood the app with messages": the other panels, the header buttons, the conversation column and the main JARVIS window still respond; "Close panel" ends it.
11. Close JARVIS and put the saved `board.json` back.

Not observable on Windows by this run: the WebRTC argument (the script already removes WebRTC; the argument is a second line behind it).

If any line says ESCAPED, any CHECK fails, the Board does not appear, the arguments are missing, anything is heard, or the loop or flood freezes the app: stop, do not ship Canvas, and report. The fallback is a separate window for each Canvas panel, with the same browser arguments.

## Result on Windows

Not yet run.
