// The Conversation column: what the person said and what Jarvis said, in order, with a
// button for each panel a Jarvis line brought, and a box to type instead of speaking.
//
// Every word here is shown as text (`textContent`), never as markup: the person's words are
// whatever the microphone heard, and Jarvis's are the model's.

import type { ConversationEntry, Panel } from "./model";

/// Lines kept on the page. The desktop keeps the whole conversation in its store.
const LINES_MAX = 300;
const TYPED_MAX = 500;
/// Sentences of one reply come within seconds of each other. A Jarvis line later than this
/// after the last is a new turn (the headline, when a panel is up).
const SAME_REPLY_MS = 15_000;
const EMPTY = "What you and Jarvis say will appear here.";

/// The lines the page holds: the stored ones, then each new one as it comes.
export class Conversation {
  private lines: ConversationEntry[] = [];
  private seen = new Set<string>();

  constructor(stored: ConversationEntry[]) {
    for (const entry of stored) this.add(entry);
  }

  private static key(entry: ConversationEntry): string {
    return `${entry.at}\n${entry.who}\n${entry.text}`;
  }

  /// False if the line was already here (it came both in the snapshot and as an event).
  add(entry: ConversationEntry): boolean {
    const key = Conversation.key(entry);
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    this.lines.push(entry);
    if (this.lines.length > LINES_MAX) {
      for (const gone of this.lines.splice(0, this.lines.length - LINES_MAX)) {
        this.seen.delete(Conversation.key(gone));
      }
    }
    return true;
  }

  entries(): ConversationEntry[] {
    return [...this.lines];
  }
}

export interface Turn {
  who: "person" | "jarvis";
  how: "voice" | "typed" | "spoken";
  at: string;
  text: string;
  panels: { id: string; title: string }[];
}

/// The conversation as turns. Jarvis speaks sentence by sentence, so sentences that follow
/// one another closely are one turn. A turn names the panels its lines brought that the Board still
/// has, each once.
export function conversationModel(entries: ConversationEntry[], panels: Panel[]): Turn[] {
  const titles = new Map(panels.map((p) => [p.panel_id, p.title]));
  const turns: Turn[] = [];
  const named = new Set<string>();
  let lastAt = Number.NaN;
  for (const entry of entries) {
    const last = turns[turns.length - 1];
    const now = Date.parse(entry.at);
    const joins =
      entry.who === "jarvis" && last?.who === "jarvis" && now - lastAt <= SAME_REPLY_MS;
    lastAt = now;
    const turn: Turn = joins
      ? last
      : { who: entry.who, how: entry.how, at: entry.at, text: "", panels: [] };
    turn.text = turn.text ? `${turn.text} ${entry.text}` : entry.text;
    for (const id of entry.panels) {
      const title = titles.get(id);
      if (title === undefined || named.has(id)) continue;
      named.add(id);
      turn.panels.push({ id, title });
    }
    if (!joins) turns.push(turn);
  }
  return turns;
}

function clock(at: string): string {
  const when = new Date(at);
  return Number.isNaN(when.getTime())
    ? ""
    : when.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function renderConversation(
  turns: Turn[],
  { onPanel }: { onPanel: (panelId: string) => void },
): HTMLElement {
  if (turns.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = EMPTY;
    return empty;
  }
  const list = document.createElement("ol");
  list.className = "turns";
  for (const turn of turns) {
    const item = document.createElement("li");
    item.className = turn.who;
    const head = document.createElement("p");
    head.className = "head";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = turn.who === "jarvis" ? "Jarvis" : turn.how === "typed" ? "You, typed" : "You";
    const time = document.createElement("time");
    time.dateTime = turn.at;
    time.textContent = clock(turn.at);
    head.append(who, time);
    const said = document.createElement("p");
    said.className = "said";
    said.textContent = turn.text;
    item.append(head, said);
    for (const { id, title } of turn.panels) {
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "marker";
      marker.textContent = title;
      marker.setAttribute("aria-label", `Show panel: ${title}`);
      marker.addEventListener("click", () => onPanel(id));
      item.append(marker);
    }
    list.append(item);
  }
  return list;
}

/// The box to type a request. `onSay` rejects if it could not be sent; the text then stays.
export function renderTyping({ onSay }: { onSay: (text: string) => Promise<void> }): HTMLFormElement {
  const form = document.createElement("form");
  form.className = "typing";
  const label = document.createElement("label");
  label.htmlFor = "typed";
  label.textContent = "Type instead of speaking";
  const input = document.createElement("input");
  input.id = "typed";
  input.type = "text";
  input.maxLength = TYPED_MAX;
  input.autocomplete = "off";
  input.spellcheck = false;
  form.append(label, input);
  let sending = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || sending) return; // a second Enter while the first is on its way
    sending = true;
    void onSay(text)
      .then(
        () => {
          if (input.value.trim() === text) input.value = "";
        },
        () => {},
      )
      .finally(() => {
        sending = false;
      });
  });
  return form;
}
