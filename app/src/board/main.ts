// The Board window. It draws what the desktop's store holds and follows its events; every
// change the person makes goes back through the app's commands.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Conversation, conversationModel, renderConversation, renderTyping } from "./conversation";
import { fillBody, LINK_REFUSED } from "./frames";
import { tile } from "./layout";
import {
  BoardModel,
  type BoardEvent,
  type BoardSnapshot,
  type ConversationEntry,
  type PanelState,
} from "./model";
import { formatRupees, renderClosed, renderDock, renderPanel } from "./panels";
import { Receipts } from "./receipts";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const area = $("panels");
const shelf = $("shelf");
const FOCUS_SHOWN_MS = 1500;

interface Mounted {
  el: HTMLElement;
  revision: number;
  state: PanelState;
  title: string;
}
const mounted = new Map<string, Mounted>();
const receipts = new Receipts();

let model = new BoardModel([]);
let talk = new Conversation([]);
/// What the Conversation column was last built from (lines and panel titles).
let talkBuiltFor = "";
/// What the shelf (dock and closed list) was last built from. It is rebuilt only when that
/// changes, so a redraw for any other reason never wipes an open "Clear closed panels?".
let shelfBuiltFor = "";
/// Events that came while the snapshot was being fetched: applied after it, in order.
let loading = false;
const waiting: BoardEvent[] = [];
let focusTimer: ReturnType<typeof setTimeout> | undefined;

function problem(text: string | null) {
  const line = $("problem");
  line.hidden = text === null;
  line.textContent = text ?? "";
}

function report(id: string, revision: number, ok: boolean) {
  void invoke("board_rendered", { panelId: id, revision, ok }).catch(() => {});
}

/// Only one panel is maximised at a time, and a panel Jarvis shows or points at must be
/// seen: any other maximised panel steps back to open.
function uncover(id: string) {
  for (const other of model.covering(id)) void setState(other, "open");
}

async function setState(id: string, state: PanelState) {
  model.setState(id, state);
  if (state === "maximised") uncover(id);
  draw();
  try {
    await invoke("board_set_state", { panelId: id, state });
    problem(null);
  } catch {
    // The desktop did not record it: show what it really holds.
    await load();
    problem("That change could not be saved.");
  }
}

async function clearClosed() {
  try {
    await invoke<number>("board_clear_closed");
    await load(); // what the desktop now holds
  } catch {
    problem("The closed panels could not be cleared.");
    draw();
  }
}

const actions = { onState: setState, onClear: clearClosed };

function mount(id: string) {
  const panel = model.get(id);
  if (!panel) return;
  const fresh = renderPanel(panel, actions);
  const old = mounted.get(id);
  if (old && old.revision === panel.revision && old.title === panel.title) {
    // Only its state changed: a new header, the same body (a frame is never redrawn for that).
    old.el.dataset.state = panel.state;
    old.el.querySelector("header")?.replaceWith(fresh.querySelector("header")!);
    old.state = panel.state;
    return;
  }
  if (old) {
    old.el.replaceWith(fresh);
    receipts.removed(id);
  } else {
    area.append(fresh);
  }
  mounted.set(id, { el: fresh, revision: panel.revision, state: panel.state, title: panel.title });
  const body = fresh.querySelector<HTMLElement>(".body")!;
  const revision = panel.revision;
  void fillBody(body, panel).then((ok) => {
    if (mounted.get(id)?.el !== fresh) return; // replaced or removed meanwhile
    if (receipts.filled(id, revision, ok)) report(id, revision, ok);
  });
}

/// Put each panel in view where the tiling says. Run alone when only the window's size
/// changed.
function layout() {
  const rects = tile(
    model.inView().map((p) => ({ id: p.panel_id, state: p.state })),
    area.clientWidth,
    area.clientHeight,
  );
  for (const r of rects) {
    const el = mounted.get(r.id)?.el;
    if (!el) continue;
    el.style.left = `${r.x}px`;
    el.style.top = `${r.y}px`;
    el.style.width = `${r.width}px`;
    el.style.height = `${r.height}px`;
  }
  return rects;
}

function draw() {
  const inView = model.inView();
  const rects = tile(
    inView.map((p) => ({ id: p.panel_id, state: p.state })),
    area.clientWidth,
    area.clientHeight,
  );
  const shown = new Set(rects.map((r) => r.id));
  for (const [id, m] of mounted) {
    if (!shown.has(id)) {
      m.el.remove(); // minimised, closed, covered or gone: its frame leaves the page
      mounted.delete(id);
      receipts.removed(id);
    }
  }
  for (const r of rects) {
    const panel = model.get(r.id)!;
    const m = mounted.get(r.id);
    if (!m || m.revision !== panel.revision || m.state !== panel.state || m.title !== panel.title) {
      mount(r.id);
    }
    mounted.get(r.id)!.el.classList.toggle("focused", model.focused === r.id);
  }
  layout();
  $("nothing").hidden = model.all().length > 0;

  const shelved = [...model.docked(), ...model.closed()]
    .map((p) => `${p.panel_id}:${p.state}:${p.title}`)
    .join("\n");
  if (shelved !== shelfBuiltFor) {
    shelfBuiltFor = shelved;
    shelf.replaceChildren(
      renderDock(model.docked(), actions),
      renderClosed(model.closed(), actions),
    );
  }
  drawTalk(); // panel titles may have changed
  const docked = model.docked().length;
  $("counts").textContent = `${inView.length} open` + (docked ? `, ${docked} minimised` : "");
  $("spend").textContent = `Today: ${formatRupees(model.spendTodayPaise(new Date()))}`;
}

/// A panel's button in the Conversation column: bring that panel back if it is put away,
/// and point at it.
function showFromTalk(id: string) {
  const panel = model.get(id);
  if (!panel) return;
  if (panel.state === "minimised" || panel.state === "closed") void setState(id, "open");
  uncover(id);
  model.focused = id;
  pointAt(id);
}

function drawTalk() {
  const turns = conversationModel(talk.entries(), model.all());
  const built = JSON.stringify(turns);
  if (built === talkBuiltFor) return;
  talkBuiltFor = built;
  const aside = $("conversation");
  // Follow the newest line only if the person was already at the end, not while reading back.
  const atEnd = aside.scrollHeight - aside.scrollTop - aside.clientHeight < 40;
  $("talk").replaceChildren(renderConversation(turns, { onPanel: showFromTalk }));
  if (atEnd) aside.scrollTop = aside.scrollHeight;
}

async function say(text: string) {
  try {
    await invoke("board_say", { text });
    problem(null);
  } catch (e) {
    // The app says why in plain words (Jarvis is busy, or not listening).
    problem(typeof e === "string" && e.length < 120 ? e : "That could not be sent.");
    throw e;
  }
}

/// Bring the panel Jarvis pointed at into sight and mark it for a moment.
function pointAt(id: string) {
  draw();
  mounted.get(id)?.el.scrollIntoView({ block: "nearest" });
  clearTimeout(focusTimer);
  focusTimer = setTimeout(() => {
    model.focused = null;
    draw();
  }, FOCUS_SHOWN_MS);
}

function onEvent(event: BoardEvent) {
  if (loading) {
    waiting.push(event);
    return;
  }
  if (!model.apply(event)) return;
  const id = event.panel_id;
  const arrival = event.type === "show" || event.type === "update";
  if (arrival || event.type === "focus" || event.state === "maximised") uncover(id);
  if (arrival || event.type === "focus") pointAt(id);
  else draw();
  // After drawing: a panel sent again at the revision already on the page is reported now.
  if (arrival && receipts.arrived(id, event.revision)) report(id, event.revision, true);
}

async function load() {
  loading = true;
  try {
    const snapshot = await invoke<BoardSnapshot>("board_snapshot");
    model = new BoardModel(snapshot.panels);
    const stored = new Conversation(snapshot.conversation);
    for (const entry of talk.entries()) stored.add(entry); // lines that came while loading
    talk = stored;
    // A store that somehow holds two maximised panels: the newest keeps it.
    const top = model.inView().find((p) => p.state === "maximised");
    if (top) for (const other of model.covering(top.panel_id)) model.setState(other, "open");
    problem(null);
  } catch {
    problem("The Board could not be loaded.");
  } finally {
    loading = false;
  }
  draw();
  for (const event of waiting.splice(0)) onEvent(event);
}

document.addEventListener(LINK_REFUSED, () => problem("That link could not be opened."));
$("typing").replaceChildren(renderTyping({ onSay: say }));
await listen<BoardEvent>("jarvis://board", (e) => onEvent(e.payload));
await listen<ConversationEntry>("jarvis://board-talk", (e) => {
  if (!talk.add(e.payload)) return;
  drawTalk();
  // For a screen reader: this line only, once (the list itself is rebuilt each time).
  $("talk-latest").textContent =
    `${e.payload.who === "jarvis" ? "Jarvis" : "You"}: ${e.payload.text}`;
});
await load();
new ResizeObserver(() => layout()).observe(area);
