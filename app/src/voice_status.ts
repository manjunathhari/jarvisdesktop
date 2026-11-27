// What the line under the orb says for each `jarvis://voice_status` state, and whether the
// "Try again" button goes with it. `ready` (and anything unknown) shows nothing.

const VOICE_TEXT: Record<string, (percent: number) => string> = {
  helper_downloading: (p) => `Getting my voice helper ready, ${p}%`,
  helper_starting: () => "Starting my voice helper…",
  downloading: (p) => `Getting my voice ready, ${p}%`,
  loading: () => "Loading my voice…",
  helper_unavailable: () => "Voice helper unavailable. Using my built-in voice.",
  unavailable: () => "My voice is unavailable.",
};

export function voiceStatusText(state: string, percent: number): string {
  return VOICE_TEXT[state]?.(percent) ?? "";
}

export function voiceRetryShown(state: string): boolean {
  return state === "unavailable";
}
