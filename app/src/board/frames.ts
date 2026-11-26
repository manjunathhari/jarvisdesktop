// What goes inside a panel's body. Task 6 has no frames yet: both kinds are shown as plain
// text, so the Board, its controls and its receipts can be tried end to end. The Text frame
// (Task 7) and the sealed Canvas frame (Task 8) replace this, kind by kind.

import type { Panel } from "./model";

/// Draw `panel` into `body`. Resolves true once it is on screen, false if it could not be
/// drawn. Never throws.
export async function fillBody(body: HTMLElement, panel: Panel): Promise<boolean> {
  try {
    const pre = document.createElement("pre");
    pre.className = "unframed";
    pre.textContent = panel.content; // text, never markup
    body.replaceChildren(pre);
    return true;
  } catch {
    body.replaceChildren();
    body.textContent = "This panel couldn't be shown.";
    return false;
  }
}
