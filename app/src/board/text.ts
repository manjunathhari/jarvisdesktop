// The Text frame: a panel's Markdown as safe HTML.
//
// Panel content is untrusted (Jarvis wrote it, partly from web pages). So:
// - HTML in the content is never HTML here: it is shown as the text it is.
// - Only a plain `https:` link is a link, and it carries no `href`: the Board never navigates.
//   A click asks the app to open the system browser. The address is shown beside the link.
// - No image is ever loaded (that would call out to the network): it is named instead.
// - What is about to go into the page is checked once more, and anything unexpected is a
//   failed drawing, not a drawn one.

import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";
import MarkdownIt from "markdown-it";

// A short, fixed list: colouring is a nicety, and some grammars are slow on hostile input.
for (const [name, grammar] of Object.entries({
  bash,
  css,
  javascript,
  json,
  python,
  rust,
  sql,
  typescript,
  yaml,
})) {
  hljs.registerLanguage(name, grammar);
}

const LINK_MAX = 2048;
/// Code longer than this, or with a longer line, is shown plain: colouring it could stall
/// the Board (the work grows faster than the text).
const HIGHLIGHT_MAX = 4000;
const HIGHLIGHT_LINE_MAX = 300;
/// More elements than this is a failed drawing, not a Board that stops answering.
const ELEMENTS_MAX = 40_000;

/// The address a link may open, or null. The app checks again with the same rules
/// (`jarvis-node` `openable_link`; both are tested against `link-vectors.json`):
/// - at most 2,048 characters, all printable ASCII, none of `" ' < > | ^` backtick or
///   backslash, and every `%` followed by two hex digits;
/// - `https://` in lower case, then a host that starts with a letter, digit or `[`;
/// - no `@` and no `%` before the path (no name, no password, no disguised host);
/// - a URL parser agrees it is an https address with a host;
/// - and the host is out on the internet, not this PC or a private network.
export function linkTarget(raw: string): string | null {
  if (raw.length > LINK_MAX) return null;
  if (!/^https:\/\/[A-Za-z0-9[][\x21-\x7e]*$/.test(raw)) return null;
  if (/["'<>|^`\\]/.test(raw) || /%(?![0-9A-Fa-f]{2})/.test(raw)) return null;
  const authority = raw.slice(8).split(/[/?#]/, 1)[0];
  if (authority.includes("@") || authority.includes("%")) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password) return null;
  return publicHost(url.hostname) ? raw : null;
}

/// Names that never mean a host on the internet.
const LOCAL_SUFFIXES = ["localhost", "local", "lan", "internal", "intranet", "home", "corp", "home.arpa"];

/// Whether a link's host is out on the internet, as far as its name or number shows (the
/// app's `public_host` has the same rules). A panel is written from web pages and has no
/// business linking to the person's own devices: this PC, the home network, a router.
function publicHost(hostname: string): boolean {
  if (hostname.startsWith("[")) return false; // any IPv6 number
  const name = hostname.replace(/\.+$/, "").toLowerCase();
  const quad = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(name);
  if (quad) {
    const [a, b] = [Number(quad[1]), Number(quad[2])];
    return !(
      a === 0 ||
      a === 10 ||
      a === 127 ||
      a >= 224 ||
      (a === 100 && b >= 64 && b < 128) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b < 32) ||
      (a === 192 && b === 168)
    );
  }
  if (!name.includes(".")) return false;
  if (LOCAL_SUFFIXES.some((s) => name === s || name.endsWith(`.${s}`))) return false;
  return !/^\d+$/.test(name.slice(name.lastIndexOf(".") + 1)); // a number the parser did not take as one
}

const md = new MarkdownIt({
  html: false,
  linkify: false,
  typographer: false,
  highlight(code, language) {
    const colourable =
      code.length <= HIGHLIGHT_MAX && code.split("\n").every((l) => l.length <= HIGHLIGHT_LINE_MAX);
    if (colourable && language && hljs.getLanguage(language)) {
      try {
        return hljs.highlight(code, { language, ignoreIllegals: true }).value;
      } catch {
        // fall through: shown plain
      }
    }
    return ""; // markdown-it escapes it
  },
});
const escape = md.utils.escapeHtml;

// Links and images are parsed only for an address that may be opened; any other stays text.
md.validateLink = (url) => linkTarget(url) !== null;

/// The links open at this point of a rendering (it is synchronous): for each, whether it
/// was written as a link at all, and the address to show after it.
let open: { link: boolean; shown: string | null }[] = [];

md.renderer.rules.link_open = (tokens, idx) => {
  // A link inside a link is not a link: its text stays, inside the outer one.
  const nested = open.some((o) => o.link);
  const target = nested ? null : linkTarget(String(tokens[idx].attrGet("href") ?? ""));
  // Only an address written out, whole and alone, as its own link text needs no second
  // showing. Anything else gets the real address beside it.
  const bare =
    tokens[idx + 1]?.type === "text" &&
    tokens[idx + 1].content === target &&
    tokens[idx + 2]?.type === "link_close";
  open.push({ link: target !== null, shown: bare ? null : target });
  return target === null ? "" : `<a role="link" tabindex="0" data-href="${escape(target)}">`;
};
md.renderer.rules.link_close = () => {
  const mine = open.pop();
  if (!mine?.link) return "";
  return mine.shown === null ? "</a>" : `</a><span class="address"> (${escape(mine.shown)})</span>`;
};
md.renderer.rules.image = (tokens, idx) => {
  const alt = tokens[idx].content.trim();
  return `<span class="no-image">[image${alt ? `: ${escape(alt)}` : ""}]</span>`;
};
// Column alignment as a class, so the page carries no inline style.
for (const cell of ["th_open", "td_open"]) {
  md.renderer.rules[cell] = (tokens, idx, options, _env, self) => {
    const token = tokens[idx];
    const align = /text-align:(left|right|center)/.exec(String(token.attrGet("style") ?? ""));
    token.attrs = (token.attrs ?? []).filter(([name]) => name !== "style");
    if (align) token.attrJoin("class", align[1]);
    return self.renderToken(tokens, idx, options);
  };
}

/// A panel's content as HTML that is safe to put in the Board.
export function renderText(content: string): string {
  open = [];
  return md.render(content);
}

const FORBIDDEN =
  "script, style, link, meta, base, iframe, frame, object, embed, img, picture, video, audio, " +
  "source, svg, math, form, input, button, select, textarea, template, slot";
const ATTRIBUTES = new Set(["class", "role", "tabindex", "data-href", "start"]);

/// Refuse anything the renderer is never meant to produce. A belt to its braces.
function check(fragment: DocumentFragment): void {
  if (fragment.querySelector(FORBIDDEN)) throw new Error("unexpected element");
  const all = fragment.querySelectorAll("*");
  if (all.length > ELEMENTS_MAX) throw new Error("too many elements");
  for (const el of all) {
    for (const { name } of [...el.attributes]) {
      if (!ATTRIBUTES.has(name)) throw new Error("unexpected attribute");
    }
  }
}

/// Draw `content` into `body` as formatted text. `openLink` is asked to open a clicked link.
/// Throws if it cannot be drawn safely; `body` is then left as it was.
export function mountText(
  body: HTMLElement,
  content: string,
  openLink: (url: string) => void,
  render: (content: string) => string = renderText,
): void {
  const template = document.createElement("template");
  template.innerHTML = render(content);
  check(template.content);
  const text = document.createElement("div");
  text.className = "text";
  text.append(template.content);
  const follow = (event: Event) => {
    const link = (event.target as Element | null)?.closest?.<HTMLElement>("a[data-href]");
    const target = link ? linkTarget(link.dataset.href ?? "") : null;
    if (!target) return;
    event.preventDefault();
    openLink(target);
  };
  text.addEventListener("click", follow);
  text.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.repeat) follow(event);
  });
  body.replaceChildren(text);
}
