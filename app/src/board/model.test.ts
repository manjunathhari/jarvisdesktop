import { describe, expect, it } from "vitest";
import { BoardModel, type Panel } from "./model";

const panel = (id: string, over: Partial<Panel> = {}): Panel => ({
  panel_id: id,
  kind: "text",
  title: `Panel ${id}`,
  content: "fictional",
  content_sha256: "0".repeat(64),
  revision: 1,
  state: "open",
  cost_paise: 100,
  created_at: "2026-11-26T10:00:00+05:30",
  ...over,
});

describe("BoardModel", () => {
  it("starts from a snapshot and lists the newest panel first", () => {
    const m = new BoardModel([panel("a"), panel("b"), panel("c", { state: "minimised" })]);
    expect(m.inView().map((p) => p.panel_id)).toEqual(["b", "a"]);
    expect(m.docked().map((p) => p.panel_id)).toEqual(["c"]);
    expect(m.closed()).toEqual([]);
  });

  it("adds a shown panel and replaces an updated one in place", () => {
    const m = new BoardModel([panel("a")]);
    m.apply({ type: "show", ...panel("b") });
    m.apply({ type: "update", ...panel("a", { revision: 2, title: "A, wider" }) });
    expect(m.inView().map((p) => [p.panel_id, p.revision, p.title])).toEqual([
      ["b", 1, "Panel b"],
      ["a", 2, "A, wider"],
    ]);
  });

  it("takes the state the event carries, as the desktop's store holds it", () => {
    const m = new BoardModel([panel("a", { state: "maximised" })]);
    m.apply({ type: "update", ...panel("a", { revision: 2, state: "maximised" }) });
    expect(m.get("a")?.state).toBe("maximised");
    m.apply({ type: "state", panel_id: "a", state: "closed" });
    expect(m.closed().map((p) => p.panel_id)).toEqual(["a"]);
    expect(m.inView()).toEqual([]);
  });

  it("focus brings a hidden panel back and marks it as the one to look at", () => {
    const m = new BoardModel([panel("a", { state: "minimised" }), panel("b", { state: "maximised" })]);
    m.apply({ type: "focus", panel_id: "a" });
    expect(m.get("a")?.state).toBe("open");
    expect(m.focused).toBe("a");
    m.apply({ type: "focus", panel_id: "b" });
    expect(m.get("b")?.state).toBe("maximised");
  });

  it("ignores an event for a panel it does not hold, except a show or an update", () => {
    const m = new BoardModel([]);
    m.apply({ type: "state", panel_id: "x", state: "closed" });
    m.apply({ type: "focus", panel_id: "x" });
    expect(m.all()).toEqual([]);
    m.apply({ type: "update", ...panel("x", { revision: 3 }) });
    expect(m.all().map((p) => p.panel_id)).toEqual(["x"]);
  });

  it("names the panels that hide a new arrival: another one that is maximised", () => {
    const m = new BoardModel([panel("a", { state: "maximised" }), panel("b")]);
    m.apply({ type: "show", ...panel("c") });
    expect(m.covering("c")).toEqual(["a"]);
    expect(m.covering("a")).toEqual([]);
    m.setState("a", "open");
    expect(m.covering("c")).toEqual([]);
  });

  it("clearing removes the closed panels only", () => {
    const m = new BoardModel([panel("a", { state: "closed" }), panel("b"), panel("c", { state: "closed" })]);
    m.clearClosed();
    expect(m.all().map((p) => p.panel_id)).toEqual(["b"]);
  });

  it("adds up today's spend in India from the panels it holds", () => {
    const m = new BoardModel([
      panel("a", { created_at: "2026-11-26T00:00:00+05:30", cost_paise: 100 }),
      panel("b", { created_at: "2026-11-26T18:29:59Z", cost_paise: 20 }),
      panel("c", { created_at: "2026-11-25T23:59:59+05:30", cost_paise: 4000 }),
      panel("d", { created_at: "2026-11-26T18:30:00Z", cost_paise: 8000 }),
    ]);
    expect(m.spendTodayPaise(new Date("2026-11-26T17:00:00Z"))).toBe(120);
  });

  it("a maximised panel is covered by another maximised one", () => {
    const m = new BoardModel([panel("a", { state: "maximised" }), panel("b", { state: "maximised" })]);
    expect(m.covering("b")).toEqual(["a"]);
    expect(m.covering("a")).toEqual(["b"]);
  });

  it("nothing covers a panel that is itself out of view", () => {
    const m = new BoardModel([panel("a", { state: "maximised" }), panel("b", { state: "minimised" })]);
    expect(m.covering("b")).toEqual([]);
  });

  it("ignores an arrival older than what it holds", () => {
    const m = new BoardModel([panel("a", { revision: 3, title: "Third" })]);
    expect(m.apply({ type: "update", ...panel("a", { revision: 2, title: "Second" }) })).toBe(false);
    expect(m.get("a")?.title).toBe("Third");
    expect(m.apply({ type: "update", ...panel("a", { revision: 3, title: "Third" }) })).toBe(true);
  });
});
