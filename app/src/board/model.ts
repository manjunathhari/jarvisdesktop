// The Board window's own picture of the panels. Pure: no DOM, no calls to the app.
//
// The desktop's store (jarvis-node `BoardStore`) is what is true on this PC. The window starts
// from its snapshot and then follows its events, each of which carries a panel as the store
// holds it. A click changes the model at once and is sent to the app, which records it.

export type PanelKind = "text" | "canvas";
export type PanelState = "open" | "minimised" | "maximised" | "closed";

export interface Panel {
  panel_id: string;
  kind: PanelKind;
  title: string;
  content: string;
  content_sha256: string;
  revision: number;
  state: PanelState;
  cost_paise: number;
  created_at: string;
}

export type BoardEvent =
  | ({ type: "show" } & Panel)
  | ({ type: "update" } & Panel)
  | { type: "state"; panel_id: string; state: PanelState }
  | { type: "focus"; panel_id: string };

export interface ConversationEntry {
  at: string;
  who: "person" | "jarvis";
  text: string;
  how: "voice" | "typed" | "spoken";
  panels: string[];
}

export interface BoardSnapshot {
  panels: Panel[];
  conversation: ConversationEntry[];
  spend_today_paise: number;
  /// Whether Jarvis is working on something for the Board now.
  working?: boolean;
}

const IST_MS = (5 * 60 + 30) * 60 * 1000;

/// The calendar day in India of an instant, as YYYY-MM-DD.
function dayInIndia(at: Date): string {
  return new Date(at.getTime() + IST_MS).toISOString().slice(0, 10);
}

export class BoardModel {
  /// In the order the panels first arrived (the store's order).
  private panels: Panel[];
  /// The panel Jarvis last asked the person to look at.
  focused: string | null = null;

  constructor(panels: readonly Panel[]) {
    this.panels = panels.map((p) => ({ ...p }));
  }

  all(): Panel[] {
    return [...this.panels];
  }

  get(id: string): Panel | undefined {
    return this.panels.find((p) => p.panel_id === id);
  }

  private newestFirst(states: readonly PanelState[]): Panel[] {
    return this.panels.filter((p) => states.includes(p.state)).reverse();
  }

  /// Open and maximised panels, newest first: what `tile` lays out.
  inView(): Panel[] {
    return this.newestFirst(["open", "maximised"]);
  }

  docked(): Panel[] {
    return this.newestFirst(["minimised"]);
  }

  closed(): Panel[] {
    return this.newestFirst(["closed"]);
  }

  /// Follow an event from the store. False: it changed nothing (an arrival older than what
  /// is held, or a panel this Board does not have).
  apply(event: BoardEvent): boolean {
    if (event.type === "show" || event.type === "update") {
      const { type: _type, ...panel } = event;
      const at = this.panels.findIndex((p) => p.panel_id === panel.panel_id);
      if (at >= 0 && this.panels[at].revision > panel.revision) return false;
      if (at >= 0) this.panels[at] = panel;
      else this.panels.push(panel);
      this.focused = panel.panel_id;
      return true;
    }
    const held = this.get(event.panel_id);
    if (!held) return false;
    if (event.type === "state") {
      held.state = event.state;
    } else {
      // As the store does: a hidden panel comes back; one in view keeps its state.
      if (held.state === "minimised" || held.state === "closed") held.state = "open";
      this.focused = held.panel_id;
    }
    return true;
  }

  /// A click on this Board: the model follows at once.
  setState(id: string, state: PanelState): void {
    const held = this.get(id);
    if (held) held.state = state;
  }

  /// The panels that keep `id` from being seen though it is in view: any other that is
  /// maximised. Only one panel is maximised at a time; the Board steps these back to open.
  covering(id: string): string[] {
    const mine = this.get(id);
    if (!mine || (mine.state !== "open" && mine.state !== "maximised")) return [];
    return this.panels
      .filter((p) => p.panel_id !== id && p.state === "maximised")
      .map((p) => p.panel_id);
  }

  clearClosed(): void {
    this.panels = this.panels.filter((p) => p.state !== "closed");
  }

  /// What today's panels have cost (the day in India), as the desktop's snapshot counts it.
  spendTodayPaise(now: Date): number {
    const today = dayInIndia(now);
    return this.panels
      .filter((p) => dayInIndia(new Date(p.created_at)) === today)
      .reduce((sum, p) => sum + p.cost_paise, 0);
  }
}
