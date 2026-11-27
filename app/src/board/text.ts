// The Text frame: a panel's Markdown as safe HTML.
//
// Panel content is untrusted (Jarvis wrote it, partly from web pages). So:
// - HTML in the content is never HTML here: it is shown as the text it is.
// - Only a plain `https:` link is a link, and it carries no `href`: the Board never navigates.
//   A click asks the app to open the system browser. The address is shown beside the link.
// - No image is ever loaded (that would call out to the network): it is named instead.
// - What is about to go into the page is checked once more, and anything unexpected is a
//   failed drawing, not a drawn one.

import hljs from "highlight.js/lib/common";
import MarkdownIt from "markdown-it";

const LINK_MAX = 2048;

/// The address a link may open, or null: `https:` only, with a host, no name or password in
/// it, no spaces or control characters, and not absurdly long.
export function linkTarget(raw: string): string | null {
  if (raw.length > LINK_MAX || !/^https:\/\/[^\s\u0000-\u001f\u007f]+$/i.test(raw)) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password) return null;
  return raw;
}

const md = new MarkdownIt({
  html: false,
  linkify: false,
  typographer: false,
  highlight(code, language) {
    if (language && hljs.getLanguage(language)) {
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
  const target = linkTarget(String(tokens[idx].attrGet("href") ?? ""));
  // An address written out as its own link text needs no second showing.
  const bare = tokens[idx + 1]?.type === "text" && tokens[idx + 1].content === target;
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
  for (const el of fragment.querySelectorAll("*")) {
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
    if (event.key === "Enter") follow(event);
  });
  body.replaceChildren(text);
}
