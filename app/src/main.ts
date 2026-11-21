// The UI only draws what Rust sends it (ADR-001). It never fetches anything
// itself, and inserts text only via textContent — never HTML.
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

type Card = { number?: number; title: string; source: string; summary: string; link: string };
type SlotView =
  | { slot: string; kind: "card_list"; title: string; cards: Card[] }
  | { slot: string; kind: "card_detail"; card: Card }
  | { slot: string; kind: "video"; title: string; embed_url: string };
type ScreenView = { panels: SlotView[] };

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const wall = $("wall");
const overlay = $("overlay");
const heard = $("heard");
const input = document.getElementById("devinput") as HTMLInputElement | null;
const said = $("said");

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Keep the live video element across updates so the stream doesn't restart.
let videoSrc = "";

function render(view: ScreenView) {
  const keepVideo = wall.querySelector<HTMLElement>('[data-slot="primary"]');
  const nextVideo = view.panels.find((p) => p.kind === "video");
  const reuse = keepVideo && nextVideo && nextVideo.kind === "video" && nextVideo.embed_url === videoSrc;

  wall.replaceChildren();
  overlay.replaceChildren();
  overlay.hidden = true;

  for (const p of view.panels) {
    if (p.kind === "card_detail") {
      const a = el("article");
      a.append(el("p", "src", p.card.source), el("h1", "", p.card.title), el("p", "body", p.card.summary));
      overlay.append(a);
      overlay.hidden = false;
      continue;
    }
    if (p.kind === "video") {
      if (reuse && keepVideo) {
        wall.append(keepVideo);
        continue;
      }
      const sec = el("section", "panel");
      sec.dataset.slot = p.slot;
      const box = el("div", "video");
      const f = document.createElement("iframe");
      f.src = p.embed_url;
      f.title = p.title;
      f.allow = "autoplay; encrypted-media; picture-in-picture";
      f.referrerPolicy = "strict-origin-when-cross-origin";
      // Ask the YouTube player to report events (errors) back to us.
      f.addEventListener("load", () =>
        f.contentWindow?.postMessage(JSON.stringify({ event: "listening", id: 1, channel: "widget" }), "*"),
      );
      box.append(f, el("p", "cap", p.title));
      sec.append(box);
      wall.append(sec);
      videoSrc = p.embed_url;
      continue;
    }
    const sec = el("section", "panel");
    sec.dataset.slot = p.slot;
    sec.append(el("h2", "", p.title));
    const ol = el("ol", "stories");
    for (const c of p.cards) {
      const li = el("li", "story");
      li.append(el("span", "n", String(c.number ?? "")), el("span", "t", c.title), el("span", "src", c.source));
      ol.append(li);
    }
    sec.append(ol);
    wall.append(sec);
  }
  if (!nextVideo) videoSrc = "";
}

// A channel that refuses to play inside the app (embedding disabled, offline,
// wrong id) is reported once; Rust then switches to the next channel.
let reportedFor = "";
window.addEventListener("message", (e) => {
  if (!/^https:\/\/www\.youtube\.com$/.test(e.origin)) return;
  let msg: { event?: string; info?: unknown };
  try {
    msg = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
  } catch {
    return;
  }
  if (msg?.event === "onError" && videoSrc && reportedFor !== videoSrc) {
    reportedFor = videoSrc;
    invoke("video_failed");
  }
});

// ── stand-in voice: the system voice via the web view. Used only until
// George's voice (Kokoro, in Rust) is downloaded and loaded.
const synth = window.speechSynthesis;
let voice: SpeechSynthesisVoice | undefined;
function pickVoice() {
  const vs = synth?.getVoices() ?? [];
  voice = vs.find((v) => v.lang === "en-GB" && /male|george|ryan|daniel/i.test(v.name)) ?? vs.find((v) => v.lang === "en-GB") ?? vs[0];
}
synth?.addEventListener?.("voiceschanged", pickVoice);
pickVoice();

function speak(text: string) {
  said.textContent = text;
  if (!synth) return;
  const u = new SpeechSynthesisUtterance(text);
  if (voice) u.voice = voice;
  u.rate = 1.02;
  synth.speak(u);
}

// ── wiring
listen<ScreenView>("jarvis://screen", (e) => render(e.payload));
listen<string>("jarvis://speak", (e) => speak(e.payload));
listen<string>("jarvis://caption", (e) => (said.textContent = e.payload));
const voiceStatus = $("voicestatus");
listen<{ state: string; percent: number }>("jarvis://ears_status", ({ payload }) => {
  const text: Record<string, string> = {
    downloading: `Getting my hearing ready, ${payload.percent}%`,
    loading: "Loading my hearing…",
    no_mic: "I can't find a microphone. You can still type.",
  };
  voiceStatus.textContent = text[payload.state] ?? "";
  voiceStatus.hidden = !text[payload.state];
  if (payload.state === "ready") input?.setAttribute("placeholder", "Speak, or type here and press Enter");
});
listen<{ state: string; percent: number }>("jarvis://voice_status", ({ payload }) => {
  const text: Record<string, string> = {
    downloading: `Getting my voice ready, ${payload.percent}%`,
    loading: "Loading my voice…",
  };
  voiceStatus.textContent = text[payload.state] ?? "";
  voiceStatus.hidden = !text[payload.state];
});
listen("jarvis://stop_speaking", () => synth?.cancel());
listen<string>("jarvis://heard", (e) => {
  heard.textContent = e.payload;
  said.textContent = "";
});
// Turn the live TV down while JARVIS is listening, thinking or speaking, so
// you can hear George and the microphone hears you, not the TV.
function setTvVolume(volume: number) {
  const f = wall.querySelector<HTMLIFrameElement>('[data-slot="primary"] iframe');
  f?.contentWindow?.postMessage(JSON.stringify({ event: "command", func: "setVolume", args: [volume] }), "*");
}
const TV_DUCKED = 8;
const TV_NORMAL = 100;

listen<string>("jarvis://status", (e) => {
  document.body.dataset.mode = e.payload;
  setTvVolume(["listening", "thinking", "follow_up"].includes(e.payload) ? TV_DUCKED : TV_NORMAL);
  if (e.payload === "listening" && !heard.textContent) heard.textContent = "Listening…";
  if (e.payload === "idle") heard.textContent = "";
});
listen<boolean>("jarvis://mic", (e) => (document.body.dataset.mic = e.payload ? "on" : "off"));

// First run: ask for the key once, then JARVIS restarts itself.
if (await invoke<boolean>("needs_key")) {
  const setup = $<HTMLFormElement>("setup");
  const input = $<HTMLInputElement>("setupkey");
  const err = $("setuperror");
  setup.hidden = false;
  input.focus();
  setup.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    try {
      await invoke("save_key", { key: input.value });
    } catch (msg) {
      err.textContent = String(msg);
    }
  });
}

// Tapping anywhere wakes JARVIS (except inside the video, which keeps its own controls).
document.addEventListener("pointerdown", (e) => {
  if ((e.target as HTMLElement).closest("#devtype, #setup")) return;
  invoke("tap");
});
// Esc always closes JARVIS; F11 switches between window and full screen.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") invoke("quit");
  if (e.key === "F11") {
    e.preventDefault();
    invoke("toggle_fullscreen");
  }
});
$("orb").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") invoke("tap");
});

// Dev builds: type instead of speaking (no microphone until Milestone B).
if (import.meta.env.DEV || (await invoke<boolean>("typed_input_enabled"))) {
  const form = $<HTMLFormElement>("devtype");
  const input = $<HTMLInputElement>("devinput");
  form.hidden = false;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text) invoke("say", { text });
    input.value = "";
  });
}
