/**
 * markdown.ts: a small, safe markdown renderer for note cards. [SHARED]
 *
 * Notes may come from an agent, so the renderer builds DOM nodes and sets text, never HTML:
 * headings, paragraphs, bullet and numbered lists, **bold**, *italic*, `code`, and http(s)
 * links (opened in a new tab without access back to this page). Anything else shows as text.
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

export function renderMarkdown(source: string): HTMLElement {
  const root = h("div", { class: "note-body" });
  let list: HTMLElement | null = null;
  let paragraph: string[] = [];
  const flush = (): void => {
    if (paragraph.length) root.append(h("p", {}, ...inline(paragraph.join(" "))));
    paragraph = [];
  };
  for (const raw of source.split(/\r?\n/)) {
    const line = raw.trimEnd();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      list = null;
      root.append(h(heading[1]!.length === 1 ? "h1" : heading[1]!.length === 2 ? "h2" : "h3", {}, ...inline(heading[2]!)));
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
