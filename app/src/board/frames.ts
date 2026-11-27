// What goes inside a panel's body, by kind. A text panel gets the Text frame. A canvas panel
// is still shown as its source text: the sealed Canvas frame (Task 8) replaces that.

import { invoke } from "@tauri-apps/api/core";
import type { Panel } from "./model";
import { mountText, renderText } from "./text";

/// Sent on `document` when a clicked link could not be opened, so the Board can say so.
export const LINK_REFUSED = "board-link-refused";

/// Ask the app to open an address in the system browser. The app checks it again.
function openLink(url: string): void {
  void invoke("board_open_link", { url }).catch(() => {
    document.dispatchEvent(new CustomEvent(LINK_REFUSED));
  });
}

/// Draw `panel` into `body`. Resolves true once it is on the page, false if it could not be
/// drawn (the body then says so). Never throws.
export async function fillBody(
  body: HTMLElement,
  panel: Panel,
  render: (content: string) => string = renderText,
): Promise<boolean> {
  try {
    if (panel.kind === "text") {
      mountText(body, panel.content, openLink, render);
    } else {
      const pre = document.createElement("pre");
      pre.className = "unframed";
      pre.textContent = panel.content; // text, never markup
      body.replaceChildren(pre);
    }
    return true;
  } catch {
    body.replaceChildren();
    body.textContent = "This panel couldn't be shown.";
    return false;
  }
}
