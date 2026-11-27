import { describe, expect, it } from "vitest";
import { voiceRetryShown, voiceStatusText } from "./voice_status";

describe("the voice status line", () => {
  it("has the exact words for every state", () => {
    expect(voiceStatusText("helper_downloading", 42)).toBe("Getting my voice helper ready, 42%");
    expect(voiceStatusText("helper_starting", 0)).toBe("Starting my voice helper…");
    expect(voiceStatusText("downloading", 7)).toBe("Getting my voice ready, 7%");
    expect(voiceStatusText("loading", 100)).toBe("Loading my voice…");
    expect(voiceStatusText("helper_unavailable", 0)).toBe("Voice helper unavailable. Using my built-in voice.");
    expect(voiceStatusText("unavailable", 0)).toBe("My voice is unavailable.");
  });

  it("hides the line when ready or unknown", () => {
    expect(voiceStatusText("ready", 100)).toBe("");
    expect(voiceStatusText("something_new", 0)).toBe("");
  });

  it("offers Try again only when no voice is left", () => {
    expect(voiceRetryShown("unavailable")).toBe(true);
    for (const s of ["ready", "helper_unavailable", "helper_starting", "helper_downloading", "downloading", "loading"]) {
      expect(voiceRetryShown(s)).toBe(false);
    }
  });
});
