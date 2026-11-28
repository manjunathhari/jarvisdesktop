import { describe, expect, it } from "vitest";
import { LatestVoiceStatus, hearingStatusText, statusArea, voiceRetryShown, voiceStatusText } from "./voice_status";

const s = (state: string, percent = 0) => ({ state, percent });
const NO_MIC = "I can't find a microphone. You can still type.";
const HELPER_UNAVAILABLE = "Voice helper unavailable. Using my built-in voice.";
const UNAVAILABLE = "My voice is unavailable.";

describe("the voice status line", () => {
  it("has the exact words for every state", () => {
    expect(voiceStatusText("helper_downloading", 42)).toBe("Getting my voice helper ready, 42%");
    expect(voiceStatusText("helper_starting", 0)).toBe("Starting my voice helper…");
    expect(voiceStatusText("downloading", 7)).toBe("Getting my voice ready, 7%");
    expect(voiceStatusText("loading", 100)).toBe("Loading my voice…");
    expect(voiceStatusText("helper_unavailable", 0)).toBe(HELPER_UNAVAILABLE);
    expect(voiceStatusText("unavailable", 0)).toBe(UNAVAILABLE);
  });

  it("keeps the hearing's words", () => {
    expect(hearingStatusText("downloading", 3)).toBe("Getting my hearing ready, 3%");
    expect(hearingStatusText("loading", 100)).toBe("Loading my hearing…");
    expect(hearingStatusText("no_mic", 0)).toBe(NO_MIC);
    expect(hearingStatusText("ready", 100)).toBe("");
  });

  it("hides the line when ready or unknown", () => {
    expect(voiceStatusText("ready", 100)).toBe("");
    expect(voiceStatusText("something_new", 0)).toBe("");
  });

  it("offers Try again only when no voice is left", () => {
    expect(voiceRetryShown("unavailable")).toBe(true);
    for (const st of ["ready", "helper_unavailable", "helper_starting", "helper_downloading", "downloading", "loading"]) {
      expect(voiceRetryShown(st)).toBe(false);
    }
  });
});

describe("the status area", () => {
  it("shows a lasting voice line and no_mic together, voice first", () => {
    expect(statusArea(s("helper_unavailable"), s("no_mic")).lines).toEqual([HELPER_UNAVAILABLE, NO_MIC]);
    expect(statusArea(s("unavailable"), s("no_mic")).lines).toEqual([UNAVAILABLE, NO_MIC]);
    expect(statusArea(s("unavailable"), s("downloading", 5)).lines).toEqual([UNAVAILABLE, "Getting my hearing ready, 5%"]);
  });

  it("keeps a lasting voice line once the hearing is ready", () => {
    expect(statusArea(s("helper_unavailable"), s("ready", 100)).lines).toEqual([HELPER_UNAVAILABLE]);
    expect(statusArea(s("unavailable"), null).lines).toEqual([UNAVAILABLE]);
  });

  it("shows Try again for unavailable whatever the hearing says", () => {
    for (const h of [null, s("downloading", 1), s("loading"), s("no_mic"), s("ready", 100)]) {
      expect(statusArea(s("unavailable"), h).retry).toBe(true);
    }
  });

  it("has no Try again for helper_unavailable or any other voice state", () => {
    for (const v of [null, s("helper_unavailable"), s("ready", 100), s("loading"), s("helper_starting")]) {
      expect(statusArea(v, s("no_mic")).retry).toBe(false);
    }
  });

  it("shows only the hearing line beside a ready voice", () => {
    expect(statusArea(s("ready", 100), s("no_mic")).lines).toEqual([NO_MIC]);
    expect(statusArea(s("ready", 100), s("ready", 100)).lines).toEqual([]);
  });

  it("shows one transient line at a time, the hearing's first, as before", () => {
    expect(statusArea(s("loading"), s("downloading", 9)).lines).toEqual(["Getting my hearing ready, 9%"]);
    expect(statusArea(s("downloading", 50), null).lines).toEqual(["Getting my voice ready, 50%"]);
    expect(statusArea(s("helper_starting"), s("ready", 100)).lines).toEqual(["Starting my voice helper…"]);
    expect(statusArea(null, null).lines).toEqual([]);
  });
});

describe("the on-load voice status", () => {
  it("is applied when no event came first", () => {
    const g = new LatestVoiceStatus();
    expect(g.snapshot(s("helper_unavailable"))).toEqual(s("helper_unavailable"));
  });

  it("is ignored once an event has arrived", () => {
    const g = new LatestVoiceStatus();
    expect(g.event(s("ready", 100))).toEqual(s("ready", 100));
    expect(g.snapshot(s("loading"))).toBeNull();
  });
});
