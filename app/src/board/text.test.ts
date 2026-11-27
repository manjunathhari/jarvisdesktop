// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { fillBody } from "./frames";
import type { Panel } from "./model";
import { linkTarget, mountText, renderText } from "./text";

const dom = (content: string): HTMLElement => {
  const box = document.createElement("div");
  box.innerHTML = renderText(content);
  return box;
};

const panel = (over: Partial<Panel> = {}): Panel => ({
  panel_id: "a",
  kind: "text",
  title: "Fictional",
  content: "# Heading\n\nBody.",
  content_sha256: "0".repeat(64),
  revision: 1,
  state: "open",
  cost_paise: 0,
  created_at: "2026-11-26T10:00:00+05:30",
  ...over,
});

describe("renderText", () => {
  it("shows raw HTML in content as text, never as elements", () => {
    const box = dom(
      'Before <script>window.hacked = 1</script> <img src=x onerror="window.hacked = 1"> ' +
        '<iframe src="https://example.org"></iframe> <a href="https://example.org" onclick="x()">t</a>\n\n' +
        "<div onclick=\"x()\">block</div>\n\n<style>body{display:none}</style>",
    );
    expect(box.querySelector("script, img, iframe, style, div, [onclick], [onerror]")).toBeNull();
    expect(box.textContent).toContain("<script>window.hacked = 1</script>");
    expect(box.textContent).toContain('<img src=x onerror="window.hacked = 1">');
    expect(box.querySelectorAll("a").length).toBe(0);
  });

  it("renders headings, lists, emphasis, tables and code blocks", () => {
    const box = dom(
      "## Options\n\n**Pick** the *first*.\n\n- one\n- two\n\n" +
        "| Place | Nights |\n|:--|--:|\n| Coorg | 3 |\n\n" +
        "```python\nprint('<b>hi</b>')\n```\n\n```nosuchlanguage\na < b\n```\n\n`inline <i>`",
    );
    expect(box.querySelector("h2")?.textContent).toBe("Options");
    expect(box.querySelector("strong")?.textContent).toBe("Pick");
    expect(box.querySelectorAll("li").length).toBe(2);
    expect(box.querySelectorAll("table tbody tr").length).toBe(1);
    expect(box.querySelector("td.right")?.textContent).toBe("3");
    expect(box.querySelector("[style]")).toBeNull();
    const [known, unknown] = [...box.querySelectorAll("pre code")];
    expect(known.textContent).toBe("print('<b>hi</b>')\n");
    expect(known.querySelector("span")).not.toBeNull(); // highlighted
    expect(known.querySelector("b")).toBeNull();
    expect(unknown.textContent).toBe("a < b\n");
    expect(box.querySelector("i")).toBeNull();
  });

  it("makes only https links clickable, and shows where each goes", () => {
    const box = dom(
      "[good](https://example.org/a?b=1) [plain](http://example.org) [js](javascript:alert(1)) " +
        "[file](file:///C:/x) [data](data:text/html,x) [rel](/etc) [anchor](#top) " +
        "<https://example.org/auto> <javascript:alert(2)> [caps](JAVASCRIPT:alert(3)) " +
        "[odd](https://user:pw@example.org) [tab](java\tscript:alert(4))",
    );
    const links = [...box.querySelectorAll<HTMLElement>("a")];
    expect(links.map((a) => a.dataset.href)).toEqual([
      "https://example.org/a?b=1",
      "https://example.org/auto",
    ]);
    for (const a of links) {
      expect(a.hasAttribute("href")).toBe(false); // the Board itself never navigates
      expect(a.getAttribute("role")).toBe("link");
      expect(a.tabIndex).toBe(0);
    }
    expect(box.querySelector(".address")?.textContent).toContain("example.org");
    expect(box.textContent).toContain("javascript:alert(1)"); // still readable, as text
    expect(box.textContent).toContain("http://example.org"); // a refused address stays visible
    expect(box.innerHTML).not.toMatch(/\shref=/i);
  });

  it("never loads an image: it names it instead", () => {
    const box = dom("![a chart](https://example.org/c.png) ![](data:image/png;base64,AAAA)");
    expect(box.querySelector("img")).toBeNull();
    expect(box.textContent).toContain("a chart");
    expect(box.innerHTML).not.toContain("example.org/c.png\"");
  });
});

describe("linkTarget", () => {
  it("accepts only a plain https address", () => {
    expect(linkTarget("https://example.org/a")).toBe("https://example.org/a");
    for (const bad of [
      "http://example.org",
      "javascript:alert(1)",
      "file:///C:/x",
      "https://",
      "https://user:pw@example.org/",
      "https://exa mple.org",
      " https://example.org",
      "HTTPS://example.org\n",
      "https://example.org/" + "a".repeat(2100),
    ]) {
      expect(linkTarget(bad), bad).toBeNull();
    }
  });
});

describe("the Text frame in a panel", () => {
  it("opens an https link through the app, on click and on Enter, and nowhere else", () => {
    const body = document.createElement("div");
    const open = vi.fn();
    mountText(body, "[good](https://example.org/a) and **bold**", open);
    const link = body.querySelector<HTMLElement>("a")!;
    link.click();
    link.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    link.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    body.querySelector("strong")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(open.mock.calls).toEqual([["https://example.org/a"], ["https://example.org/a"]]);
  });

  it("refuses to put anything unexpected in the page, and leaves the body as it was", () => {
    for (const html of [
      '<p>ok</p><img src="x" onerror="window.hacked = 1">',
      "<script>window.hacked = 1</script>",
      '<p onclick="x()">t</p>',
      '<a href="https://example.org">t</a>',
      '<p style="position:fixed">t</p>',
      '<iframe src="https://example.org"></iframe>',
    ]) {
      const body = document.createElement("div");
      body.textContent = "before";
      expect(() => mountText(body, "x", () => {}, () => html), html).toThrow();
      expect(body.textContent).toBe("before");
    }
  });

  it("a text panel is drawn as formatted text and reported drawn", async () => {
    const body = document.createElement("div");
    expect(await fillBody(body, panel())).toBe(true);
    expect(body.querySelector(".text h1")?.textContent).toBe("Heading");
  });

  it("a canvas panel is not run as a page yet: its source is shown as text", async () => {
    const body = document.createElement("div");
    expect(await fillBody(body, panel({ kind: "canvas", content: "<svg onload='x()'></svg>" }))).toBe(true);
    expect(body.querySelector("svg")).toBeNull();
    expect(body.textContent).toContain("<svg onload='x()'></svg>");
  });

  it("a render failure reports false and says so", async () => {
    const body = document.createElement("div");
    const broken = () => {
      throw new Error("no");
    };
    expect(await fillBody(body, panel(), broken)).toBe(false);
    expect(body.textContent).toBe("This panel couldn't be shown.");
    expect(body.children.length).toBe(0);
  });
});
