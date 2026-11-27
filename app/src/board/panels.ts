// The Board's panel shell: the header every panel has, the dock of minimised panels and the
// list of closed ones. Everything from a panel is put in the page as text (`textContent`),
// never as markup. What goes inside a panel's body is the frames' work (Tasks 7 and 8).

import type { Panel, PanelState } from "./model";

export interface PanelActions {
  onState: (panelId: string, state: PanelState) => void;
}

export interface ClosedActions extends PanelActions {
  /// The person confirmed: remove the closed panels from this screen.
  onClear: () => void;
}

const KIND_LABEL: Record<Panel["kind"], string> = { text: "Text", canvas: "Canvas" };

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(className: string, label: string, text: string, onClick: () => void) {
  const b = el("button", className, text);
  b.type = "button";
  b.setAttribute("aria-label", label);
  b.addEventListener("click", onClick);
  return b;
}

export function formatRupees(paise: number): string {
  const rupees = Math.floor(paise / 100).toLocaleString("en-IN");
  return `Rs ${rupees}.${String(paise % 100).padStart(2, "0")}`;
}

/// One panel: a header (title, kind, and the three controls) and an empty body.
export function renderPanel(panel: Panel, actions: PanelActions): HTMLElement {
  const section = el("section", "panel");
  section.dataset.panelId = panel.panel_id;
  section.dataset.state = panel.state;
  section.dataset.kind = panel.kind;
  section.setAttribute("aria-label", panel.title);

  const header = el("header");
  header.append(el("span", "kind", KIND_LABEL[panel.kind]), el("h2", undefined, panel.title));
  const controls = el("div", "controls");
  const id = panel.panel_id;
  const maximised = panel.state === "maximised";
  controls.append(
    button("minimise", "Minimise panel", "–", () => actions.onState(id, "minimised")),
    maximised
      ? button("restore", "Restore panel", "❐", () => actions.onState(id, "open"))
      : button("maximise", "Maximise panel", "□", () => actions.onState(id, "maximised")),
    button("close", "Close panel", "×", () => actions.onState(id, "closed")),
  );
  header.append(controls);
  section.append(header, el("div", "body"));
  return section;
}

/// The minimised panels, each a button that brings it back.
export function renderDock(minimised: readonly Panel[], actions: PanelActions): HTMLElement {
  const dock = el("nav", "dock");
  dock.setAttribute("aria-label", "Minimised panels");
  dock.hidden = minimised.length === 0;
  for (const p of minimised) {
    dock.append(
      button("restore", `Restore panel: ${p.title}`, p.title, () =>
        actions.onState(p.panel_id, "open"),
      ),
    );
  }
  return dock;
}

/// The closed panels, each with Reopen, and "Clear closed panels" behind a second step.
export function renderClosed(closed: readonly Panel[], actions: ClosedActions): HTMLElement {
  const box = el("section", "closed");
  box.setAttribute("aria-label", "Recently closed panels");
  box.hidden = closed.length === 0;
  box.append(el("h2", undefined, "Recently closed"));
  const list = el("ul");
  for (const p of closed) {
    const item = el("li");
    item.append(
      el("span", "title", p.title),
      button("reopen", `Reopen panel: ${p.title}`, "Reopen", () =>
        actions.onState(p.panel_id, "open"),
      ),
    );
    list.append(item);
  }
  box.append(list);

  const clear = button("clear", "Clear closed panels", "Clear closed panels", () => {
    if (box.querySelector(".confirm")) return;
    const ask = el("div", "confirm");
    ask.setAttribute("role", "alertdialog");
    ask.setAttribute("aria-label", "Clear closed panels?");
    const count = closed.length === 1 ? "1 closed panel" : `${closed.length} closed panels`;
    ask.append(
      el("p", undefined, `Remove ${count} from this screen? Jarvis still keeps them.`),
      button("confirm-clear", "Yes, clear them", "Clear", () => actions.onClear()),
      button("cancel", "Cancel", "Cancel", () => ask.remove()),
    );
    box.append(ask);
    ask.querySelector<HTMLButtonElement>("button.cancel")?.focus();
  });
  box.append(clear);
  return box;
}
