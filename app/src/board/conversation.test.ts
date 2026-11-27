// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { Conversation, conversationModel, renderConversation, renderTyping } from "./conversation";
import type { ConversationEntry, Panel } from "./model";

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 27, 5, 0, seconds)).toISOString();
const line = (
  seconds: number,
  who: "person" | "jarvis",
  text: string,
  over: Partial<ConversationEntry> = {},
): ConversationEntry => ({
  at: at(seconds),
  who,
  text,
  how: who === "jarvis" ? "spoken" : "voice",
  panels: [],
  ...over,
});
const panel = (id: string, title: string, over: Partial<Panel> = {}): Panel => ({
  panel_id: id,
  kind: "text",
  title,
  content: "x",
  content_sha256: "0".repeat(64),
  revision: 1,
  state: "open",
  cost_paise: 0,
  created_at: at(0),
  ...over,
});

describe("conversationModel", () => {
  it("keeps the order and marks each of the person's lines voice or typed", () => {
    const turns = conversationModel(
      [
        line(1, "person", "show the fictional rainfall"),
        line(2, "jarvis", "On it."),
        line(30, "person", "add a column", { how: "typed" }),
        line(31, "jarvis", "Done."),
      ],
      [],
    );
    expect(turns.map((t) => [t.who, t.how, t.text])).toEqual([
      ["person", "voice", "show the fictional rainfall"],
      ["jarvis", "spoken", "On it."],
      ["person", "typed", "add a column"],
      ["jarvis", "spoken", "Done."],
    ]);
  });

  it("joins the sentences Jarvis speaks one after another into one turn", () => {
    const turns = conversationModel(
      [
        line(1, "person", "compare them"),
        line(2, "jarvis", "On it."),
        line(3, "jarvis", "I'll put it on the board."),
        line(60, "jarvis", "Three options are up.", { panels: ["a"] }),
        line(61, "jarvis", "Coorg looks best."),
      ],
      [panel("a", "December trip")],
    );
    expect(turns.map((t) => t.text)).toEqual([
      "compare them",
      "On it. I'll put it on the board.",
      "Three options are up. Coorg looks best.",
    ]);
    expect(turns[2].panels).toEqual([{ id: "a", title: "December trip" }]);
    expect(turns[1].panels).toEqual([]);
  });

  it("names only panels the Board still has, each once", () => {
    const turns = conversationModel(
      [line(1, "jarvis", "Up.", { panels: ["a", "gone", "a"] }), line(2, "jarvis", "And.", { panels: ["a", "b"] })],
      [panel("a", "Rainfall"), panel("b", "Flights", { state: "closed" })],
    );
    expect(turns[0].panels).toEqual([
      { id: "a", title: "Rainfall" },
      { id: "b", title: "Flights" },
    ]);
  });
});

describe("the Conversation column", () => {
  it("shows both sides as text, says who spoke and how, and never as markup", () => {
    const box = renderConversation(
      conversationModel(
        [line(1, "person", "<img src=x onerror=1> hello", { how: "typed" }), line(2, "jarvis", "**Hi** <b>there</b>")],
        [],
      ),
      { onPanel: () => {} },
    );
    expect(box.querySelector("img, b, strong")).toBeNull();
    const [mine, theirs] = [...box.querySelectorAll("li")];
    expect(mine.className).toContain("person");
    expect(mine.querySelector(".who")?.textContent).toBe("You, typed");
    expect(mine.querySelector(".said")?.textContent).toBe("<img src=x onerror=1> hello");
    expect(theirs.querySelector(".who")?.textContent).toBe("Jarvis");
    expect(theirs.querySelector(".said")?.textContent).toBe("**Hi** <b>there</b>");
    expect(mine.querySelector("time")?.getAttribute("datetime")).toBe(at(1));
  });

  it("a panel marker is a button that focuses its panel", () => {
    const onPanel = vi.fn();
    const box = renderConversation(
      conversationModel([line(1, "jarvis", "Up.", { panels: ["a"] })], [panel("a", "Rainfall")]),
      { onPanel },
    );
    const marker = box.querySelector<HTMLButtonElement>("button.marker")!;
    expect(marker.textContent).toBe("Rainfall");
    expect(marker.getAttribute("aria-label")).toBe("Show panel: Rainfall");
    marker.click();
    expect(onPanel).toHaveBeenCalledWith("a");
  });

  it("shows that Jarvis is working, after the last line, only while it is", () => {
    const turns = conversationModel([line(1, "person", "compare them"), line(2, "jarvis", "On it.")], []);
    const idle = renderConversation(turns, { onPanel: () => {} });
    expect(idle.querySelector(".working")).toBeNull();
    const busy = renderConversation(turns, { onPanel: () => {}, working: true });
    const last = busy.querySelector("li:last-child")!;
    expect(last.className).toBe("working");
    expect(last.textContent).toBe("Jarvis is working…");
    expect(busy.querySelectorAll("li").length).toBe(3);
    // Even before anything has been said.
    const first = renderConversation([], { onPanel: () => {}, working: true });
    expect(first.querySelector(".working")?.textContent).toBe("Jarvis is working…");
    expect(first.textContent).not.toContain("will appear here");
  });

  it("says so when nothing has been said yet", () => {
    const box = renderConversation([], { onPanel: () => {} });
    expect(box.textContent).toBe("What you and Jarvis say will appear here.");
  });
});

describe("Conversation", () => {
  it("takes the stored lines, then new ones, and never the same line twice", () => {
    const talk = new Conversation([line(1, "person", "one"), line(2, "jarvis", "two")]);
    expect(talk.add(line(2, "jarvis", "two"))).toBe(false); // already in the snapshot
    expect(talk.add(line(3, "person", "three"))).toBe(true);
    expect(talk.entries().map((e) => e.text)).toEqual(["one", "two", "three"]);
  });

  it("keeps only the newest lines on the page", () => {
    const many = Array.from({ length: 450 }, (_, i) => line(i, "person", `line ${i}`));
    const talk = new Conversation(many);
    expect(talk.entries().length).toBe(300);
    expect(talk.entries()[0].text).toBe("line 150");
  });
});

describe("typing instead of speaking", () => {
  it("has a labelled box; Enter sends the text once and clears it", async () => {
    const onSay = vi.fn(async () => {});
    const form = renderTyping({ onSay });
    const input = form.querySelector<HTMLInputElement>("input")!;
    expect(form.querySelector("label")?.textContent).toBe("Type instead of speaking");
    expect(form.querySelector("label")?.getAttribute("for")).toBe(input.id);
    expect(input.maxLength).toBe(500);
    input.value = "  add a column  ";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await Promise.resolve();
    expect(onSay).toHaveBeenCalledWith("add a column");
    expect(input.value).toBe("");
  });

  it("a second Enter while the first is being sent sends nothing more", async () => {
    let release = () => {};
    const onSay = vi.fn(() => new Promise<void>((done) => (release = done)));
    const form = renderTyping({ onSay });
    form.querySelector<HTMLInputElement>("input")!.value = "once";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(onSay).toHaveBeenCalledTimes(1);
    release();
    await new Promise((r) => setTimeout(r, 0));
    form.querySelector<HTMLInputElement>("input")!.value = "again";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(onSay).toHaveBeenCalledTimes(2);
  });

  it("sends nothing for an empty box, and keeps the text if sending failed", async () => {
    const onSay = vi.fn(async () => {
      throw new Error("no");
    });
    const form = renderTyping({ onSay });
    const input = form.querySelector<HTMLInputElement>("input")!;
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(onSay).not.toHaveBeenCalled();
    input.value = "keep me";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await new Promise((r) => setTimeout(r, 0));
    expect(input.value).toBe("keep me");
  });
});
