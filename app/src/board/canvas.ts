// The Canvas frame: a panel that is a small web page, shown sealed.
//
// The page is untrusted. It is never put into the Board's own document: it is served by the
// app's Canvas server on the loopback (`jarvis-node` `canvas.rs`, which says why and carries
// the page's own policy) and shown here in a frame whose sandbox grants scripts and nothing
// else. The panel's header and buttons are outside the frame.
//
// "Drawn" is reported only when the frame has loaded AND the server confirms it handed out
// this panel's page at this revision: a frame also "loads" an empty refusal.

import type { Panel } from "./model";

/// The three ports the Canvas server may use (`canvas::PORTS`); the app's content policy
/// allows frames from these origins only.
const PORTS = "47821|47822|47823";
/// The policy the Canvas server sends with every page (`canvas::POLICY`, word for word; a
/// test checks). The frame demands it too, so that if some other program were answering on
/// one of the three ports, the browser would refuse to show its page.
export const POLICY =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'; sandbox allow-scripts";
const FAILED = "This panel couldn't be shown.";

/// What the frame needs from the app.
export interface CanvasApp {
  /// The address of the panel's page at that revision. Rejects if there is no server.
  address(panelId: string, revision: number): Promise<string>;
  /// Whether the server really handed that page out.
  served(panelId: string, revision: number): Promise<boolean>;
}

/// `address` if it is exactly this panel's page on the Canvas server, else null.
export function canvasAddress(address: string, panelId: string, revision: number): string | null {
  const shape = new RegExp(`^http://127\\.0\\.0\\.1:(${PORTS})/[0-9a-f]{32}/([0-9a-f-]{36})/([0-9]+)$`);
  const found = shape.exec(address);
  return found && found[2] === panelId && found[3] === String(revision) ? address : null;
}

/// The frame for a page. Scripts only: no same-origin, forms, pop-ups, top navigation,
/// downloads or device access. The title is for screen readers.
export function canvasFrame(address: string, title: string): HTMLIFrameElement {
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("csp", POLICY);
  frame.title = title;
  frame.className = "canvas";
  frame.setAttribute("src", address);
  return frame;
}

const wait = (ms: number) => new Promise((done) => setTimeout(done, ms));

/// Show `panel` in `body`. Resolves true once its page is on screen, false if it could not
/// be shown (the body then says so). Never throws.
export async function mountCanvas(
  body: HTMLElement,
  panel: Panel,
  app: CanvasApp,
  { timeoutMs = 5000, retryMs = 250 } = {},
): Promise<boolean> {
  const failed = () => {
    body.classList.remove("framed");
    body.replaceChildren();
    body.textContent = FAILED;
    return false;
  };
  let address: string | null;
  try {
    address = canvasAddress(await app.address(panel.panel_id, panel.revision), panel.panel_id, panel.revision);
  } catch {
    address = null;
  }
  if (address === null) return failed();

  const frame = canvasFrame(address, panel.title);
  const arrived = new Promise<boolean>((done) => {
    const timer = setTimeout(() => done(false), timeoutMs);
    const end = (ok: boolean) => {
      clearTimeout(timer);
      done(ok);
    };
    frame.addEventListener("load", () => end(true), { once: true });
    frame.addEventListener("error", () => end(false), { once: true });
  });
  body.classList.add("framed");
  body.replaceChildren(frame);
  if (!(await arrived)) return frame.isConnected ? failed() : false;
  // Taken off the page meanwhile (closed, minimised, replaced): nothing was drawn, and the
  // body is no longer this frame's to write in.
  if (!frame.isConnected) return false;
  try {
    // Exactly `true`: nothing else the app's channel might hand back counts.
    let served = (await app.served(panel.panel_id, panel.revision)) === true;
    if (!served) {
      await wait(retryMs); // the answer may still be on its way out
      served = (await app.served(panel.panel_id, panel.revision)) === true;
    }
    if (served) return frame.isConnected;
  } catch {
    // fall through
  }
  return frame.isConnected ? failed() : false;
}
