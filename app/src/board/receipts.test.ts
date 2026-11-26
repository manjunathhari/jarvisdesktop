import { describe, expect, it } from "vitest";
import { Receipts } from "./receipts";

describe("Receipts", () => {
  it("owes a report for a panel that arrived, once it is drawn", () => {
    const r = new Receipts();
    expect(r.arrived("a", 1)).toBe(false);
    expect(r.filled("a", 1, true)).toBe(true);
    expect(r.filled("a", 1, true)).toBe(false); // exactly once
  });

  it("owes nothing for a panel drawn from the snapshot", () => {
    const r = new Receipts();
    expect(r.filled("a", 1, true)).toBe(false);
  });

  it("reports at once a panel sent again at the revision already on the page", () => {
    const r = new Receipts();
    r.filled("a", 2, true);
    expect(r.arrived("a", 2)).toBe(true);
    expect(r.filled("a", 2, true)).toBe(false); // nothing left owing
  });

  it("does not report a newer revision on the strength of an older drawing", () => {
    const r = new Receipts();
    r.filled("a", 1, true);
    expect(r.arrived("a", 2)).toBe(false);
    expect(r.filled("a", 1, true)).toBe(false); // the old drawing finishing late
    expect(r.filled("a", 2, true)).toBe(true);
  });

  it("forgets a drawing that left the page but still owes its report", () => {
    const r = new Receipts();
    r.filled("a", 1, true);
    r.removed("a");
    expect(r.arrived("a", 1)).toBe(false); // not on the page: nothing to claim
    expect(r.filled("a", 1, true)).toBe(true); // back on the page
  });

  it("a drawing that failed is owed as a failure and never counts as drawn", () => {
    const r = new Receipts();
    r.arrived("a", 1);
    expect(r.filled("a", 1, false)).toBe(true);
    expect(r.arrived("a", 1)).toBe(false);
  });
});
