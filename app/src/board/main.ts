// The Board window. It draws what the desktop's store holds and follows its events; every
// change the person makes goes back through the app's commands.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { fillBody } from "./frames";
import { tile } from "./layout";
import { BoardModel, type BoardEvent, type BoardSnapshot, type PanelState } from "./model";
import { formatRupees, renderClosed, renderDock, renderPanel } from "./panels";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const area = $("panels");
const shelf = $("shelf");

interface Mounted {
  el: HTMLElement;
  revision: number;
  state: PanelState;
  title: string;
}
const mounted = new Map<string, Mounted>();
/// Panels that arrived by an event and whose receipt waits for them to be drawn.
const awaiting = new Map<string, number>();

let model = new BoardModel([]);
/// What the shelf (dock and closed list) was last built from. It is rebuilt only when that
/// changes, so a redraw for any other reason never wipes an open "Clear closed panels?".
let shelfBuiltFor = "";

function problem(text: string | null) {
  const line = $("problem");
  line.hidden = text === null;
  line.textContent = text ?? "";
}

async function setState(id: string, state: PanelState) {
  model.setState(id, state);
  draw();
  try {
    await invoke("board_set_state", { panelId: id, state });
    problem(null);
  } catch {
    // The desktop did not record it: show what it really holds.
    problem("That change could not be saved.");
    await load();
  }
}

async function clearClosed() {
  try {
    await invoke<number>("board_clear_closed");
    model.clearClosed();
    problem(null);
  } catch {
    problem("The closed panels could not be cleared.");
  }
  draw();
}

const actions = { onState: setState, onClear: clearClosed };

function mount(id: string) {
  const panel = model.get(id);
  if (!panel) return;
  const fresh = renderPanel(panel, actions);
  const now: Mounted = { el: fresh, revision: panel.revision, state: panel.state, title: panel.title };
  const old = mounted.get(id);
  if (old && old.revision === panel.revision && old.title === panel.title) {
    // Only its state changed: a new header, the same body (a frame is never redrawn for that).
    old.el.dataset.state = panel.state;
    old.el.querySelector("header")?.replaceWith(fresh.querySelector("header")!);
    old.state = panel.state;
    return;
  }
  if (old) old.el.replaceWith(fresh);
  else area.append(fresh);
  mounted.set(id, now);
  const body = fresh.querySelector<HTMLElement>(".body")!;
  const revision = panel.revision;
  void fillBody(body, panel).then((ok) => {
    // Report only a panel that came by an event, and only while this drawing is still the
    // one on screen.
    if (awaiting.get(id) !== revision || mounted.get(id)?.el !== fresh) return;
    awaiting.delete(id);
    void invoke("board_rendered", { panelId: id, revision, ok }).catch(() => {});
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
  const open = inView.length;
  const docked = model.docked().length;
  $("counts").textContent =
    `${open} open` + (docked ? `, ${docked} minimised` : "");
  $("spend").textContent = `Today: ${formatRupees(model.spendTodayPaise(new Date()))}`;

  if (model.focused) {
    const id = model.focused;
    mounted.get(id)?.el.scrollIntoView({ block: "nearest" });
    setTimeout(() => {
      if (model.focused === id) {
        model.focused = null;
        mounted.get(id)?.el.classList.remove("focused");
      }
    }, 1500);
  }
}

function onEvent(event: BoardEvent) {
  model.apply(event);
  if (event.type === "show" || event.type === "update") {
    awaiting.set(event.panel_id, event.revision);
    // A new arrival must be seen to be reported drawn: a panel that covers it steps back.
    for (const other of model.covering(event.panel_id)) void setState(other, "open");
  }
  draw();
}

async function load() {
  const snapshot = await invoke<BoardSnapshot>("board_snapshot");
  model = new BoardModel(snapshot.panels);
  draw();
}

await listen<BoardEvent>("jarvis://board", (e) => onEvent(e.payload));
await load();
new ResizeObserver(() => layout()).observe(area);
