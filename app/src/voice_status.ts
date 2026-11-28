// The status area under the orb: what the voice and the hearing report there, which lines are
// shown, whether the "Try again" button goes with them, and which answer about the voice wins.

const VOICE_TEXT: Record<string, (percent: number) => string> = {
  helper_downloading: (p) => `Getting my voice helper ready, ${p}%`,
  helper_starting: () => "Starting my voice helper…",
  downloading: (p) => `Getting my voice ready, ${p}%`,
  loading: () => "Loading my voice…",
  helper_unavailable: () => "Voice helper unavailable. Using my built-in voice.",
  unavailable: () => "My voice is unavailable.",
};

const HEARING_TEXT: Record<string, (percent: number) => string> = {
  downloading: (p) => `Getting my hearing ready, ${p}%`,
  loading: () => "Loading my hearing…",
  no_mic: () => "I can't find a microphone. You can still type.",
};

/// The voice states that last: they are never hidden by a hearing line.
const LASTING_VOICE = new Set(["helper_unavailable", "unavailable"]);

export type StatusView = { state: string; percent: number };

export function voiceStatusText(state: string, percent: number): string {
  return VOICE_TEXT[state]?.(percent) ?? "";
}

export function hearingStatusText(state: string, percent: number): string {
  return HEARING_TEXT[state]?.(percent) ?? "";
}

export function voiceRetryShown(state: string): boolean {
  return state === "unavailable";
}

/// Given the latest voice and hearing statuses (null: none yet), the lines to show (in order)
/// and whether "Try again" is shown. A lasting voice line comes first and stays beside any
/// hearing line; otherwise one line, the hearing's while it has one. The button follows the
/// voice alone.
export function statusArea(voice: StatusView | null, hearing: StatusView | null): { lines: string[]; retry: boolean } {
  const v = voice ? voiceStatusText(voice.state, voice.percent) : "";
  const h = hearing ? hearingStatusText(hearing.state, hearing.percent) : "";
  const lasting = voice !== null && LASTING_VOICE.has(voice.state);
  const lines = lasting ? [v, h].filter(Boolean) : [h || v].filter(Boolean);
  return { lines, retry: voice !== null && voiceRetryShown(voice.state) };
}

/// The page asks for the voice's status once on load, but events and invoke answers are not
/// ordered: once any event has arrived, the on-load answer is older and is ignored.
export class LatestVoiceStatus {
  private eventSeen = false;

  /// A `jarvis://voice_status` event: always applied.
  event(s: StatusView): StatusView {
    this.eventSeen = true;
    return s;
  }

  /// The on-load answer: applied only if no event came first.
  snapshot(s: StatusView): StatusView | null {
    return this.eventSeen ? null : s;
  }
}
