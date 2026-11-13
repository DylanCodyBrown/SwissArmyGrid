/**
 * markdown.ts: a small, safe markdown renderer for note cards. [SHARED]
 *
 * Notes may come from an agent, so the renderer builds DOM nodes and sets text, never HTML:
 * headings, paragraphs, bullet and numbered lists, tables (GitHub style: a header row, a |---| rule,
 * numbers right-aligned), fenced code blocks, quotes, rules (---), **bold**, *italic*, `code`, and
 * http(s) links (opened in a new tab without access back to this page). Anything else shows as text.
 */
import { h } from "./dom";

function inline(text: string): Node[] {
  const out: Node[] = [];
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const token = match[0];
    if (match.index > last) out.push(document.createTextNode(text.slice(last, match.index)));
    if (token.startsWith("**")) out.push(h("strong", {}, token.slice(2, -2)));
    else if (token.startsWith("`")) out.push(h("code", {}, token.slice(1, -1)));
    else if (token.startsWith("[")) {
      const [, label, href] = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(token) ?? [];
      out.push(h("a", { href, target: "_blank", rel: "noopener noreferrer" }, label ?? token));
    } else out.push(h("em", {}, token.slice(1, -1)));
    last = match.index + token.length;
  }
  if (last < text.length) out.push(document.createTextNode(text.slice(last)));
  return out;
}

/** A table row's cells: `| a | b |` or `a | b`, with `\|` for a bar inside a cell. */
function cells(line: string): string[] {
  const body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return body.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

const RULE_ROW = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const NUMBER = /^[-+]?[$€£]?[\d.,]+(%|[a-zA-Z]{0,3})?$|^[-+]?\d[\d.,]*\s*(to|–)\s*[-+]?\d[\d.,]*$/;

function table(lines: string[]): HTMLElement {
  const head = cells(lines[0]!);
  const aligns = cells(lines[1]!).map((c) => (c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : c.startsWith(":") ? "left" : ""));
  const rows = lines.slice(2).map(cells);
  // A column right-aligns when told (---:) or when every filled cell is a number.
  const numeric = head.map((_, i) => rows.length > 0 && rows.every((r) => !r[i] || NUMBER.test(r[i]!.replace(/\*\*/g, ""))));
  const align = (i: number): string => aligns[i] || (numeric[i] ? "right" : "left");
  return h(
    "div",
    { class: "md-table-wrap" },
    h(
      "table",
      { class: "md-table" },
      h("thead", {}, h("tr", {}, ...head.map((c, i) => h("th", { class: `align-${align(i)}` }, ...inline(c))))),
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...head.map((_, i) => h("td", { class: `align-${align(i)}` }, ...inline(r[i] ?? "")))))),
    ),
  );
}

export function renderMarkdown(source: string): HTMLElement {
  const root = h("div", { class: "note-body" });
  const lines = source.split(/\r?\n/);
  let list: HTMLElement | null = null;
  let paragraph: string[] = [];
  const flush = (): void => {
    if (paragraph.length) root.append(h("p", {}, ...inline(paragraph.join(" "))));
    paragraph = [];
  };
  for (let at = 0; at < lines.length; at++) {
    const line = lines[at]!.trimEnd();
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (/^\s*```/.test(line)) {
      flush();
      list = null;
      const code: string[] = [];
      for (at++; at < lines.length && !/^\s*```/.test(lines[at]!); at++) code.push(lines[at]!);
      root.append(h("pre", { class: "md-code" }, h("code", {}, code.join("\n"))));
    } else if (line.includes("|") && at + 1 < lines.length && RULE_ROW.test(lines[at + 1]!)) {
      flush();
      list = null;
      const block = [line, lines[at + 1]!];
      for (at += 2; at < lines.length && lines[at]!.includes("|") && lines[at]!.trim(); at++) block.push(lines[at]!);
      at--;
      root.append(table(block));
    } else if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      flush();
      list = null;
      root.append(h("hr", {}));
    } else if (/^\s*>\s?/.test(line)) {
      flush();
      list = null;
      const quote: string[] = [];
      for (; at < lines.length && /^\s*>\s?/.test(lines[at]!); at++) quote.push(lines[at]!.replace(/^\s*>\s?/, ""));
      at--;
      root.append(h("blockquote", {}, ...inline(quote.join(" "))));
    } else if (heading) {
      flush();
      list = null;
      const level = Math.min(4, heading[1]!.length);
      root.append(h(level === 1 ? "h1" : level === 2 ? "h2" : level === 3 ? "h3" : "h4", {}, ...inline(heading[2]!)));
    } else if (bullet || numbered) {
      flush();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = h(tag, {});
        root.append(list);
      }
      list.append(h("li", {}, ...inline((bullet ?? numbered)![1]!)));
    } else if (!line.trim()) {
      flush();
      list = null;
    } else {
      list = null;
      paragraph.push(line.trim());
    }
  }
  flush();
  return root;
}
