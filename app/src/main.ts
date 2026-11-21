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

// ── voice (Milestone A stand-in: the system voice via the web view).
// Milestone B replaces this with the local open-source voice in Rust.
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
listen("jarvis://stop_speaking", () => synth?.cancel());
listen<string>("jarvis://heard", (e) => {
  heard.textContent = e.payload;
  said.textContent = "";
});
listen<string>("jarvis://status", (e) => {
  document.body.dataset.mode = e.payload;
  if (e.payload === "idle") heard.textContent = "";
});
listen<boolean>("jarvis://mic", (e) => (document.body.dataset.mic = e.payload ? "on" : "off"));

// Tapping anywhere wakes JARVIS (except inside the video, which keeps its own controls).
document.addEventListener("pointerdown", (e) => {
  if ((e.target as HTMLElement).closest("#devtype")) return;
  invoke("tap");
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
