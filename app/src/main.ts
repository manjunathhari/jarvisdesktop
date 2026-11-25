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

// The orb's state line under it.
const ORB_WORDS: Record<string, [string, string]> = {
  idle: ["Standby", "Not listening"],
  listening: ["Listening", "Listening for your voice"],
  follow_up: ["Listening", "Listening for your voice"],
  thinking: ["Thinking", "Working on it"],
  mic_off: ["Mic off", "Tap the orb to listen"],
};
function showOrbState(mode: string) {
  const [word, hint] = ORB_WORDS[mode] ?? ORB_WORDS.idle;
  $("orbword").textContent = word;
  $("orbhint").textContent = hint;
}
showOrbState(document.body.dataset.mode ?? "idle");

// Which build this is, bottom left (from the installer's build number).
invoke<string>("build_label").then((b) => ($("build").textContent = b));

listen<string>("jarvis://status", (e) => {
  document.body.dataset.mode = e.payload;
  showOrbState(e.payload);
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
  if ((e.target as HTMLElement).closest("#devtype, #setup, #enrolment, #approvals, #notices")) return;
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

// ── Approval cards and server notices (Plan B Task 7) ──────────────────────
// What the card says next is always the server's answer: a decision is sent once, the card
// then waits until the server clears it (P2-E11). Buttons are off once time is up.
type CardView = { outbox_id: string; approval_id: string; summary: string; valid_until: string };
type Cleared = { outbox_id: string; reason: string };
type NoticeView = { outbox_id: string; priority: string; kind: string; body: string };
type OpenCard = {
  root: HTMLElement;
  until: number;
  sent: boolean;
  // The node said this card's decision went out (by tap or by voice).
  confirmed: boolean;
  clock: HTMLElement;
  note: HTMLElement;
  buttons: HTMLButtonElement[];
};

const approvals = el("section");
approvals.id = "approvals";
approvals.setAttribute("aria-live", "assertive");
const notices = el("ul");
notices.id = "notices";
document.body.append(approvals, notices);
const cards = new Map<string, OpenCard>();
const MAX_NOTICES = 5;

function clockText(left: number) {
  if (left <= 0) return "Expired";
  const m = Math.floor(left / 60);
  const s = String(left % 60).padStart(2, "0");
  return `${m}:${s} left`;
}

function tick() {
  for (const c of cards.values()) {
    const left = Math.max(0, Math.ceil((c.until - Date.now()) / 1000));
    c.clock.textContent = clockText(left);
    for (const b of c.buttons) b.disabled = c.sent || left === 0;
  }
}
setInterval(tick, 250);

async function decide(id: string, approve: boolean) {
  const c = cards.get(id);
  if (!c || c.sent) return;
  c.sent = true;
  c.note.textContent = "Sending…";
  tick();
  try {
    await invoke("decide_approval", { outboxId: id, approve });
    c.note.textContent = "Sent — waiting for Jarvis";
  } catch (msg) {
    if (c.confirmed) return; // already sent (by voice, or the other button)
    c.sent = false;
    c.note.textContent = String(msg);
  }
  tick();
}

listen<CardView>("jarvis://approval-card", ({ payload }) => {
  cards.get(payload.outbox_id)?.root.remove();
  const root = el("article", "card");
  const clock = el("p", "clock");
  const note = el("p", "note");
  const approve = el("button", "approve", "Approve");
  const deny = el("button", "deny", "Deny");
  approve.addEventListener("click", () => decide(payload.outbox_id, true));
  deny.addEventListener("click", () => decide(payload.outbox_id, false));
  const row = el("div", "buttons");
  row.append(approve, deny);
  root.append(el("h2", undefined, "Approval needed"), el("p", "summary", payload.summary), clock, row, note);
  approvals.append(root);
  cards.set(payload.outbox_id, {
    root,
    until: Date.parse(payload.valid_until),
    sent: false,
    confirmed: false,
    clock,
    note,
    buttons: [approve, deny],
  });
  tick();
});

const SENT_SHOWN_MS = 4000;

listen<Cleared>("jarvis://approval-cleared", ({ payload }) => {
  const c = cards.get(payload.outbox_id);
  if (!c) return;
  if (payload.reason === "SENT") {
    // The decision went out; what Jarvis says next is the server's answer.
    c.sent = c.confirmed = true;
    c.note.textContent = "Sent — waiting for Jarvis";
    tick();
    setTimeout(() => {
      c.root.remove();
      if (cards.get(payload.outbox_id) === c) cards.delete(payload.outbox_id);
    }, SENT_SHOWN_MS);
    return;
  }
  c.root.remove();
  cards.delete(payload.outbox_id);
});

listen<NoticeView>("jarvis://notice", ({ payload }) => {
  const li = el("li", payload.priority.toLowerCase(), payload.body);
  li.dataset.id = payload.outbox_id;
  notices.prepend(li);
  while (notices.children.length > MAX_NOTICES) notices.lastElementChild?.remove();
});

listen<Cleared>("jarvis://notice-cleared", ({ payload }) => {
  for (const li of notices.querySelectorAll<HTMLElement>("li")) {
    if (li.dataset.id === payload.outbox_id) li.remove();
  }
});

// ── The Jarvis server link and enrolment (Plan B Task 9) ──────────────────
// Status words come from Rust: claude | error: … | not_enrolled | connecting | connected |
// needs_reenrol | failed: …
const enrolment = $<HTMLFormElement>("enrolment");
const enrolCode = $<HTMLInputElement>("enrolcode");
const enrolError = $("enrolerror");
const linkLine = $("link");
const LINK_WORDS: Record<string, string> = {
  not_enrolled: "Not connected to the Jarvis server yet",
  connecting: "Connecting to the Jarvis server…",
  connected: "Jarvis server",
  needs_reenrol: "The Jarvis server needs this PC enrolled again",
};

function showLink(status: string) {
  if (status === "claude") {
    linkLine.hidden = true;
    enrolment.hidden = true;
    return;
  }
  linkLine.hidden = false;
  linkLine.textContent = LINK_WORDS[status] ?? (status.startsWith("error: ") ? status.slice(7) : status);
  linkLine.dataset.state = status === "connected" ? "connected" : status === "connecting" ? "" : "trouble";
  const enrol = status === "not_enrolled" || status === "needs_reenrol";
  if (enrol && enrolment.hidden) {
    enrolment.hidden = false;
    enrolCode.focus();
  }
  if (!enrol) enrolment.hidden = true;
}

enrolment.addEventListener("submit", async (e) => {
  e.preventDefault();
  enrolError.textContent = "";
  try {
    await invoke("enrol", { code: enrolCode.value });
    enrolCode.value = "";
  } catch (msg) {
    enrolError.textContent = String(msg);
  }
});

listen<string>("jarvis://link", ({ payload }) => showLink(payload));
showLink(await invoke<string>("node_status"));
