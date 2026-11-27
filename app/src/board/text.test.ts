// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { fillBody } from "./frames";
import type { Panel } from "./model";
import vectors from "./link-vectors.json";
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
  it("accepts only a plain https address (the list the app's own check shares)", () => {
    for (const good of vectors.open) expect(linkTarget(good), good).toBe(good);
    for (const bad of vectors.refuse) expect(linkTarget(bad), JSON.stringify(bad)).toBeNull();
    expect(linkTarget("https://example.org/" + "a".repeat(2100))).toBeNull();
  });
});

describe("hostile content", () => {
  it("always shows the real address when the link text is anything but exactly it", () => {
    for (const source of [
      "[https://paypal.co*m*/login](https://paypal.co)",
      "[https://paypal.co`m`](https://paypal.co)",
      "[https://paypal.co\\\nm](https://paypal.co)",
    ]) {
      const box = dom(source);
      expect(box.querySelector("a")?.dataset.href, source).toBe("https://paypal.co");
      expect(box.querySelector(".address")?.textContent, source).toBe(" (https://paypal.co)");
    }
    expect(dom("<https://example.org/a>").querySelector(".address")).toBeNull();
  });

  it("a link inside a link is text: one link, opening what its address says", () => {
    const box = dom("[<https://evil.example>](https://good.example)");
    const links = [...box.querySelectorAll<HTMLElement>("a")];
    expect(links.map((a) => a.dataset.href)).toEqual(["https://good.example"]);
    expect(links[0].textContent).toContain("evil.example");
    expect(box.querySelector(".address")?.textContent).toBe(" (https://good.example)");
  });

  it("reference links, titles and encoded schemes give no other kind of link", () => {
    const box = dom(
      '[a][1] [b][2] [c](https://example.org "a title") [d](&#106;avascript:alert(1)) ' +
        "[e](%6Aavascript:alert(1))\n\n[1]: https://example.org/ref\n[2]: javascript:alert(1)\n",
    );
    expect([...box.querySelectorAll<HTMLElement>("a")].map((a) => a.dataset.href)).toEqual([
      "https://example.org/ref",
      "https://example.org",
    ]);
    expect(box.querySelector("[title]")).toBeNull();
  });

  it("a fence's language cannot name a class of the Board", () => {
    const box = dom('```panel confirm" onclick="x\ncode\n```');
    const code = box.querySelector("pre code")!;
    expect([...code.classList].every((c) => c.startsWith("language-"))).toBe(true);
    expect(code.attributes.length).toBe(1);
  });

  it("draws 30,000 hostile bytes quickly", () => {
    const fill = (unit: string) => unit.repeat(Math.floor(29_900 / unit.length));
    const cases = [
      "```markdown\n" + fill("[a](") + "\n```",
      "```md\n" + fill("[a](") + "\n```",
      "```js\n" + fill("a") + "\n```",
      "```python\n" + fill("0_") + "\n```",
      "```json\n" + fill('{"a":') + "\n```",
      fill("> "),
      fill("*a "),
      fill("[a]("),
      fill("|a") + "\n|-|\n",
      fill("- a\n  "),
    ];
    for (const content of cases) {
      const started = performance.now();
      renderText(content);
      expect(performance.now() - started, content.slice(0, 16)).toBeLessThan(1500);
    }
  });

  it("a page too large to draw is a failed drawing, not a frozen Board", () => {
    const body = document.createElement("div");
    const huge = "<p>x</p>".repeat(40_001);
    expect(() => mountText(body, "x", () => {}, () => huge)).toThrow();
  });

  it("a held Enter key opens a link once", () => {
    const body = document.createElement("div");
    const open = vi.fn();
    mountText(body, "[good](https://example.org/a)", open);
    const link = body.querySelector<HTMLElement>("a")!;
    link.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    link.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, repeat: true }));
    expect(open).toHaveBeenCalledTimes(1);
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
