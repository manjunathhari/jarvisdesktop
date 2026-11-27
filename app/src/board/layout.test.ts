import { describe, expect, it } from "vitest";
import { MIN_ROW, tile } from "./layout";

const p = (id: string, state = "open") => ({ id, state });

describe("tile", () => {
  it("gives one open panel the whole area", () => {
    expect(tile([p("a")], 1200, 800)).toEqual([{ id: "a", x: 0, y: 0, width: 1200, height: 800 }]);
  });

  it("puts two open panels side by side", () => {
    expect(tile([p("a"), p("b")], 1200, 800)).toEqual([
      { id: "a", x: 0, y: 0, width: 600, height: 800 },
      { id: "b", x: 600, y: 0, width: 600, height: 800 },
    ]);
  });

  it("uses at most two columns, in the order given, and the last odd one spans the row", () => {
    const rects = tile([p("a"), p("b"), p("c")], 1200, 800);
    expect(rects).toEqual([
      { id: "a", x: 0, y: 0, width: 600, height: 400 },
      { id: "b", x: 600, y: 0, width: 600, height: 400 },
      { id: "c", x: 0, y: 400, width: 1200, height: 400 },
    ]);
  });

  it("gives a maximised panel everything and the others nothing", () => {
    expect(tile([p("a"), p("b", "maximised"), p("c")], 1200, 800)).toEqual([
      { id: "b", x: 0, y: 0, width: 1200, height: 800 },
    ]);
    // Two maximised: the first in the order given (the newest) is the one in view.
    expect(tile([p("a", "maximised"), p("b", "maximised")], 1200, 800).map((r) => r.id)).toEqual([
      "a",
    ]);
  });

  it("skips minimised and closed panels", () => {
    expect(tile([p("a", "minimised"), p("b"), p("c", "closed")], 1200, 800)).toEqual([
      { id: "b", x: 0, y: 0, width: 1200, height: 800 },
    ]);
    expect(tile([p("a", "minimised"), p("c", "closed")], 1200, 800)).toEqual([]);
    expect(tile([], 1200, 800)).toEqual([]);
  });

  it("never squeezes a row below the minimum: the area scrolls instead", () => {
    const rects = tile(["a", "b", "c", "d", "e", "f", "g"].map((id) => p(id)), 1200, 600);
    expect(rects).toHaveLength(7);
    expect(rects.every((r) => r.height === MIN_ROW)).toBe(true);
    expect(rects[6]).toEqual({ id: "g", x: 0, y: 3 * MIN_ROW, width: 1200, height: MIN_ROW });
  });

  it("copes with an area that has no size yet", () => {
    expect(tile([p("a")], 0, 0)).toEqual([{ id: "a", x: 0, y: 0, width: 0, height: MIN_ROW }]);
  });
});
