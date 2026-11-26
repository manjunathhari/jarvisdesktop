// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import type { Panel } from "./model";
import { formatRupees, renderClosed, renderDock, renderPanel } from "./panels";

const panel = (over: Partial<Panel> = {}): Panel => ({
  panel_id: "7b1d6a52-3c4e-4f8a-9b0c-1d2e3f4a5b6c",
  kind: "text",
  title: "December trip <b>options</b>",
  content: "fictional",
  content_sha256: "0".repeat(64),
  revision: 2,
  state: "open",
  cost_paise: 1240,
  created_at: "2026-11-26T10:00:00+05:30",
  ...over,
});

const labels = (el: Element) =>
  [...el.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));

describe("renderPanel", () => {
  it("has a header with the title, the kind and three labelled buttons", () => {
    const el = renderPanel(panel(), { onState: () => {} });
    expect(el.tagName).toBe("SECTION");
    expect(el.getAttribute("aria-label")).toBe("December trip <b>options</b>");
    expect(el.dataset.panelId).toBe("7b1d6a52-3c4e-4f8a-9b0c-1d2e3f4a5b6c");
    expect(el.querySelector("h2")?.textContent).toBe("December trip <b>options</b>");
    expect(el.querySelector("h2 b")).toBeNull(); // a title is text, never markup
    expect(el.querySelector(".kind")?.textContent).toBe("Text");
    expect(labels(el)).toEqual(["Minimise panel", "Maximise panel", "Close panel"]);
    expect(el.querySelector(".body")).not.toBeNull();
  });

  it("offers Restore, not Maximise, on a maximised panel", () => {
    const el = renderPanel(panel({ state: "maximised" }), { onState: () => {} });
    expect(labels(el)).toEqual(["Minimise panel", "Restore panel", "Close panel"]);
  });

  it("each button asks for its state", () => {
    const onState = vi.fn();
    const el = renderPanel(panel(), { onState });
    for (const b of el.querySelectorAll("button")) b.click();
    expect(onState.mock.calls).toEqual([
      ["7b1d6a52-3c4e-4f8a-9b0c-1d2e3f4a5b6c", "minimised"],
      ["7b1d6a52-3c4e-4f8a-9b0c-1d2e3f4a5b6c", "maximised"],
      ["7b1d6a52-3c4e-4f8a-9b0c-1d2e3f4a5b6c", "closed"],
    ]);
    const max = renderPanel(panel({ state: "maximised" }), { onState });
    (max.querySelectorAll("button")[1] as HTMLButtonElement).click();
    expect(onState.mock.calls[3]).toEqual(["7b1d6a52-3c4e-4f8a-9b0c-1d2e3f4a5b6c", "open"]);
  });

  it("labels a canvas panel as a Canvas", () => {
    const el = renderPanel(panel({ kind: "canvas" }), { onState: () => {} });
    expect(el.querySelector(".kind")?.textContent).toBe("Canvas");
  });
});

describe("the dock and the closed list", () => {
  it("the dock has one button for each minimised panel, which restores it", () => {
    const onState = vi.fn();
    const dock = renderDock([panel({ panel_id: "a", title: "Chart" })], { onState });
    const [b] = dock.querySelectorAll("button");
    expect(b.getAttribute("aria-label")).toBe("Restore panel: Chart");
    expect(b.textContent).toBe("Chart");
    b.click();
    expect(onState).toHaveBeenCalledWith("a", "open");
    expect(renderDock([], { onState }).hidden).toBe(true);
  });

  it("closed panels can be reopened, and cleared only after a second, explicit step", () => {
    const onState = vi.fn();
    const onClear = vi.fn();
    const closed = renderClosed([panel({ panel_id: "a", title: "Old trip" })], { onState, onClear });
    const reopen = closed.querySelector<HTMLButtonElement>("button.reopen")!;
    expect(reopen.getAttribute("aria-label")).toBe("Reopen panel: Old trip");
    reopen.click();
    expect(onState).toHaveBeenCalledWith("a", "open");
    const clear = closed.querySelector<HTMLButtonElement>("button.clear")!;
    expect(clear.textContent).toBe("Clear closed panels");
    clear.click();
    expect(onClear).not.toHaveBeenCalled(); // the first click only asks
    const ask = closed.querySelector(".confirm")!;
    expect(ask.textContent).toContain("Jarvis still keeps them");
    closed.querySelector<HTMLButtonElement>("button.cancel")!.click();
    expect(closed.querySelector(".confirm")).toBeNull();
    expect(onClear).not.toHaveBeenCalled();
    closed.querySelector<HTMLButtonElement>("button.clear")!.click();
    closed.querySelector<HTMLButtonElement>("button.confirm-clear")!.click();
    expect(onClear).toHaveBeenCalledTimes(1);
    expect(renderClosed([], { onState, onClear }).hidden).toBe(true);
  });
});

describe("formatRupees", () => {
  it("writes paise as rupees", () => {
    expect(formatRupees(0)).toBe("Rs 0.00");
    expect(formatRupees(1240)).toBe("Rs 12.40");
    expect(formatRupees(5)).toBe("Rs 0.05");
    expect(formatRupees(123456)).toBe("Rs 1,234.56");
  });
});
