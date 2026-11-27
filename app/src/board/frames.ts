// What goes inside a panel's body, by kind: the Text frame for a text panel, the sealed
// Canvas frame for a canvas panel.

import { invoke } from "@tauri-apps/api/core";
import { mountCanvas, type CanvasApp } from "./canvas";
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

/// The app's Canvas server, through its commands.
const canvasApp: CanvasApp = {
  address: (panelId, revision) => invoke<string>("board_canvas_address", { panelId, revision }),
  served: (panelId, revision) => invoke<boolean>("board_canvas_served", { panelId, revision }),
};

/// Draw `panel` into `body`. Resolves true once it is on the page, false if it could not be
/// drawn (the body then says so). Never throws.
export async function fillBody(
  body: HTMLElement,
  panel: Panel,
  render: (content: string) => string = renderText,
  canvas: CanvasApp = canvasApp,
): Promise<boolean> {
  if (panel.kind === "canvas") return mountCanvas(body, panel, canvas);
  try {
    mountText(body, panel.content, openLink, render);
    return true;
  } catch {
    body.replaceChildren();
    body.textContent = "This panel couldn't be shown.";
    return false;
  }
}
