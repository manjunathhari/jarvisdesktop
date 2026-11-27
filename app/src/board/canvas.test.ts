// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { canvasAddress, canvasFrame, mountCanvas, type CanvasApp } from "./canvas";
import { fillBody } from "./frames";
import type { Panel } from "./model";

const ID = "3f2c1b9e-7a44-4a8e-9d55-0c1d2e3f4a5b";
const SECRET = "0123456789abcdef0123456789abcdef";
const URL_OK = `http://127.0.0.1:47822/${SECRET}/${ID}/4`;

const panel = (over: Partial<Panel> = {}): Panel => ({
  panel_id: ID,
  kind: "canvas",
  title: "Rainfall (fictional)",
  content: "<svg onload='x()'></svg><script>window.hacked = 1</script>",
  content_sha256: "0".repeat(64),
  revision: 4,
  state: "open",
  cost_paise: 0,
  created_at: "2026-11-27T10:00:00+05:30",
  ...over,
});

const app = (over: Partial<CanvasApp> = {}): CanvasApp => ({
  address: vi.fn(async () => URL_OK),
  served: vi.fn(async () => true),
  ...over,
});

const loaded = (body: HTMLElement) =>
  body.querySelector("iframe")!.dispatchEvent(new Event("load"));

describe("canvasAddress", () => {
  it("accepts only this panel's page on one of the three loopback ports", () => {
    expect(canvasAddress(URL_OK, ID, 4)).toBe(URL_OK);
    for (const bad of [
      URL_OK.replace("47822", "47824"),
      URL_OK.replace("127.0.0.1", "localhost"),
      URL_OK.replace("http:", "https:"),
      URL_OK.replace("/4", "/5"),
      URL_OK.replace(ID, "3f2c1b9e-7a44-4a8e-9d55-000000000000"),
      URL_OK.replace(SECRET, "short"),
      URL_OK + "/",
      URL_OK + "?x=1",
      URL_OK + "#x",
      `http://127.0.0.1:47822@evil.example/${SECRET}/${ID}/4`,
      "jarvis-canvas://localhost/x",
      "javascript:alert(1)",
      "data:text/html,<script>1</script>",
      "",
    ]) {
      expect(canvasAddress(bad, ID, 4), bad).toBeNull();
    }
  });
});

describe("canvasFrame", () => {
  it("is sandboxed to scripts only, with nothing else granted", () => {
    const frame = canvasFrame(URL_OK, "Rainfall (fictional)");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(frame.getAttribute("src")).toBe(URL_OK);
    expect(frame.title).toBe("Rainfall (fictional)");
    for (const never of ["allow", "srcdoc", "allowfullscreen", "name", "csp"]) {
      expect(frame.hasAttribute(never), never).toBe(false);
    }
  });
});

describe("a canvas panel", () => {
  it("is shown in a frame, never as markup in the Board itself, and is drawn once loaded and served", async () => {
    const body = document.body.appendChild(document.createElement("div"));
    const a = app();
    const drawn = mountCanvas(body, panel(), a);
    await vi.waitFor(() => expect(body.querySelector("iframe")).not.toBeNull());
    expect(body.querySelector("svg, script")).toBeNull();
    expect(a.address).toHaveBeenCalledWith(ID, 4);
    loaded(body);
    expect(await drawn).toBe(true);
    expect(a.served).toHaveBeenCalledWith(ID, 4);
    expect(body.querySelector("iframe")).not.toBeNull();
  });

  it("is not drawn when the frame loaded something the server never handed out", async () => {
    const body = document.body.appendChild(document.createElement("div"));
    const drawn = mountCanvas(body, panel(), app({ served: vi.fn(async () => false) }), {
      retryMs: 1,
    });
    await vi.waitFor(() => expect(body.querySelector("iframe")).not.toBeNull());
    loaded(body);
    expect(await drawn).toBe(false);
    expect(body.querySelector("iframe")).toBeNull();
    expect(body.textContent).toBe("This panel couldn't be shown.");
  });

  it("asks once more before giving up, for a page still on its way out", async () => {
    const body = document.body.appendChild(document.createElement("div"));
    const served = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const drawn = mountCanvas(body, panel(), app({ served }), { retryMs: 1 });
    await vi.waitFor(() => expect(body.querySelector("iframe")).not.toBeNull());
    loaded(body);
    expect(await drawn).toBe(true);
    expect(served).toHaveBeenCalledTimes(2);
  });

  it("is not drawn if it never loads", async () => {
    const body = document.body.appendChild(document.createElement("div"));
    const drawn = mountCanvas(body, panel(), app(), { timeoutMs: 20 });
    expect(await drawn).toBe(false);
    expect(body.querySelector("iframe")).toBeNull();
    expect(body.textContent).toBe("This panel couldn't be shown.");
  });

  it("is not drawn, and no frame is made, when the app gives no address or a wrong one", async () => {
    for (const address of [
      vi.fn(async () => {
        throw new Error("canvas_unavailable");
      }),
      vi.fn(async () => "https://evil.example/"),
      vi.fn(async () => URL_OK.replace("/4", "/3")),
    ]) {
      const body = document.body.appendChild(document.createElement("div"));
      const made = vi.spyOn(document, "createElement");
      expect(await mountCanvas(body, panel(), app({ address }))).toBe(false);
      expect(made.mock.calls.some(([tag]) => tag === "iframe")).toBe(false);
      made.mockRestore();
      expect(body.textContent).toBe("This panel couldn't be shown.");
    }
  });

  it("a frame taken off the page (closed, minimised, replaced) reports nothing drawn", async () => {
    const body = document.body.appendChild(document.createElement("div"));
    const a = app();
    const drawn = mountCanvas(body, panel(), a, { timeoutMs: 400 });
    await vi.waitFor(() => expect(body.querySelector("iframe")).not.toBeNull());
    const frame = body.querySelector("iframe")!;
    frame.remove();
    frame.dispatchEvent(new Event("load"));
    expect(await drawn).toBe(false);
    expect(a.served).not.toHaveBeenCalled();
  });

  it("fillBody sends a canvas panel to the frame and a text panel to the Text frame", async () => {
    const body = document.body.appendChild(document.createElement("div"));
    const a = app();
    const drawn = fillBody(body, panel(), undefined, a);
    await vi.waitFor(() => expect(body.querySelector("iframe")).not.toBeNull());
    loaded(body);
    expect(await drawn).toBe(true);
    const text = document.body.appendChild(document.createElement("div"));
    expect(await fillBody(text, panel({ kind: "text", content: "# Hi" }), undefined, a)).toBe(true);
    expect(text.querySelector("iframe")).toBeNull();
    expect(text.querySelector("h1")?.textContent).toBe("Hi");
  });
});
