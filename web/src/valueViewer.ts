/**
 * valueViewer.ts: the cell value viewer, opened by double-clicking a cell. [WIRE + EXPORT]
 *
 * It works out what a value is and shows it the way that reads best:
 *   JSON object      a key / value table; a nested object or array opens one layer deeper
 *   array            a table when its items are objects (one column per key), else a list
 *   markdown         formatted (headings, lists, tables, code), with the raw text one tab away
 *   long text        word-wrapped, with its length
 *   short values     as they are, with their type
 * Text that holds JSON (a JSON string inside a JSON field, too) is parsed and opened the same
 * way, so a value can be explored layer by layer; the breadcrumb at the top walks back out.
 *
 * Everything is built as DOM nodes (h()), never as HTML strings, so a value can never inject
 * markup or script into the page, markdown links included (only http, https and mailto).
 *
 * ROUTING: [BREADCRUMB]   -> show(depth)          (back out to an outer layer)
 *          [OPEN BUTTON]  -> drill(label, value)  (one layer deeper)
 *          [VIEW TABS]    -> setView("formatted" | "raw")
 *          [COPY]         -> copies the current layer (pretty JSON or the text)
 *          [KIND BADGE]   -> read-only, from detect(value)
 */
import { button, clear, formatCount, h, openDialog, toast } from "./dom";

type Kind = "object" | "array" | "table" | "markdown" | "text" | "number" | "boolean" | "blank" | "short";
interface Layer {
  label: string;
  value: unknown;
}

const MAX_TABLE_ROWS = 500;
const MAX_TABLE_COLUMNS = 40;

export function openValueViewer(title: string, value: unknown): void {
  const { body, foot, close } = openDialog(title, true);
  body.classList.add("value-viewer");
  const layers: Layer[] = [{ label: title, value: parsed(value) }];
  let view: "formatted" | "raw" = "formatted";
  const crumbs = h("nav", { class: "vv-crumbs", "aria-label": "Layers" });
  const head = h("div", { class: "vv-head" });
  const content = h("div", { class: "vv-content" });
  body.append(crumbs, head, content);

  const current = (): Layer => layers[layers.length - 1]!;
  const show = (depth: number): void => {
    layers.splice(depth + 1);
    view = "formatted";
    render();
  };
  const drill = (label: string, inner: unknown): void => {
    layers.push({ label, value: parsed(inner) });
    view = "formatted";
    render();
  };
  const render = (): void => {
    const layer = current();
    const kind = detect(layer.value);
    // The dialog title names the top layer; the trail shows once there is somewhere to go back to.
    crumbs.hidden = layers.length === 1;
    clear(
      crumbs,
      ...layers.flatMap((l, i) => [
        i ? h("span", { class: "vv-sep", "aria-hidden": "true" }, "›") : null,
        i === layers.length - 1
          ? h("strong", { "aria-current": "true" }, l.label)
          : h("button", { type: "button", class: "link", onclick: () => show(i) }, l.label),
      ]),
    );
    const tabs = kind === "short" || kind === "number" || kind === "boolean" || kind === "blank" || kind === "text"
      ? []
      : [
          h("button", { class: "tab", role: "tab", "aria-selected": String(view === "formatted"), onclick: () => { view = "formatted"; render(); } }, kind === "markdown" ? "Formatted" : "Table"),
          h("button", { class: "tab", role: "tab", "aria-selected": String(view === "raw"), onclick: () => { view = "raw"; render(); } }, kind === "markdown" ? "Raw text" : "JSON"),
        ];
    clear(head, h("span", { class: "badge" }, describe(layer.value, kind)), h("span", { class: "spacer" }), tabs.length ? h("div", { class: "tabs", role: "tablist" }, ...tabs) : null);
    clear(content, view === "raw" ? raw(layer.value, kind) : formatted(layer.value, kind, drill));
  };

  foot.append(
    button("Copy", () => {
      const v = current().value;
      const text = typeof v === "string" ? v : JSON.stringify(v, null, 2);
      void navigator.clipboard.writeText(text ?? "").then(() => toast("Copied."));
    }, { icon: "copy" }),
    button("Close", close, { class: "primary" }),
  );
  render();
}

// ===== what is this value? =====

/** Text that is JSON becomes the parsed value; anything else stays as it is. */
function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!(text.startsWith("{") && text.endsWith("}")) && !(text.startsWith("[") && text.endsWith("]"))) return value;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return value;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function detect(value: unknown): Kind {
  if (value === null || value === undefined) return "blank";
  if (Array.isArray(value)) return value.length && value.every(isRecord) ? "table" : "array";
  if (isRecord(value)) return "object";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  const text = String(value);
  if (looksLikeMarkdown(text)) return "markdown";
  return text.length > 80 || text.includes("\n") ? "text" : "short";
}

export function looksLikeMarkdown(text: string): boolean {
  const strong = [/^#{1,6}\s+\S/m, /^```/m, /^\|.+\|\s*$\n^\|?\s*:?-{3,}/m];
  if (strong.some((re) => re.test(text))) return true;
  const weak = [/^\s*[-*+]\s+\S/m, /^\s*\d+\.\s+\S/m, /\*\*[^*\n]+\*\*/, /\[[^\]\n]+\]\([^)\s]+\)/, /`[^`\n]+`/, /^>\s/m];
  return weak.filter((re) => re.test(text)).length >= 2;
}

function describe(value: unknown, kind: Kind): string {
  switch (kind) {
    case "object": return `JSON object · ${plural(Object.keys(value as object).length, "key")}`;
    case "table": return `Array of ${formatCount((value as unknown[]).length)} objects`;
    case "array": return `Array · ${plural((value as unknown[]).length, "item")}`;
    case "markdown": return `Markdown · ${formatCount(String(value).length)} characters`;
    case "text": return `Text · ${formatCount(String(value).length)} characters`;
    case "number": return "Number";
    case "boolean": return "True/false";
    case "blank": return "Blank";
    default: return `Text · ${formatCount(String(value).length)} characters`;
  }
}

// ===== drawing a layer =====

function formatted(value: unknown, kind: Kind, drill: (label: string, inner: unknown) => void): HTMLElement {
  if (kind === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return h(
      "table",
      { class: "vv-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "Key"), h("th", {}, "Value"))),
      h("tbody", {}, ...entries.map(([key, v]) => h("tr", {}, h("th", { scope: "row" }, key), h("td", {}, cell(v, key, drill))))),
    );
  }
  if (kind === "table") {
    const items = value as Record<string, unknown>[];
    const keys = [...new Set(items.slice(0, MAX_TABLE_ROWS).flatMap((item) => Object.keys(item)))].slice(0, MAX_TABLE_COLUMNS);
    return h(
      "div",
      {},
      h(
        "table",
        { class: "vv-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "#"), ...keys.map((k) => h("th", {}, k)))),
        h(
          "tbody",
          {},
          ...items.slice(0, MAX_TABLE_ROWS).map((item, i) =>
            h("tr", {}, h("td", { class: "num" }, String(i)), ...keys.map((k) => h("td", {}, cell(item[k], `[${i}].${k}`, drill)))),
          ),
        ),
      ),
      items.length > MAX_TABLE_ROWS ? h("p", { class: "hint" }, `Showing the first ${MAX_TABLE_ROWS} of ${formatCount(items.length)}; the JSON tab has them all.`) : null,
    );
  }
  if (kind === "array") {
    const items = value as unknown[];
    return h(
      "table",
      { class: "vv-table" },
      h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Item"))),
      h("tbody", {}, ...items.slice(0, MAX_TABLE_ROWS).map((v, i) => h("tr", {}, h("td", { class: "num" }, String(i)), h("td", {}, cell(v, `[${i}]`, drill))))),
    );
  }
  if (kind === "markdown") return h("div", { class: "vv-markdown" }, ...renderMarkdown(String(value)));
  if (kind === "blank") return h("p", { class: "cell-blank" }, "(blank)");
  return h("pre", { class: "vv-text mono" }, String(value));
}

/** One value inside a table: scalars inline, nested values (or JSON text) behind Open. */
function cell(value: unknown, label: string, drill: (label: string, inner: unknown) => void): HTMLElement | string {
  const inner = parsed(value);
  if (inner === null || inner === undefined) return h("span", { class: "cell-blank" }, "(blank)");
  if (typeof inner === "object") {
    const summary = Array.isArray(inner) ? `[ ${plural(inner.length, "item")} ]` : `{ ${plural(Object.keys(inner).length, "key")} }`;
    return h("button", { type: "button", class: "btn small vv-open", title: `Open ${label}`, "aria-label": `Open ${label}: ${summary}`, onclick: () => drill(label, inner) }, summary, " ›");
  }
  const text = String(inner);
  if (text.length > 160 || text.includes("\n")) {
    return h("span", { class: "vv-long" }, `${text.slice(0, 160)}…`, " ", h("button", { type: "button", class: "link", onclick: () => drill(label, text) }, "Open"));
  }
  return h("span", { class: typeof inner === "number" ? "num" : "" }, text);
}

function raw(value: unknown, kind: Kind): HTMLElement {
  if (kind === "markdown") return h("pre", { class: "vv-text mono" }, String(value));
  return h("pre", { class: "vv-json mono" }, ...highlightJson(JSON.stringify(value, null, 2) ?? ""));
}

/** "1 item", "2 items". */
const plural = (n: number, word: string): string => `${formatCount(n)} ${word}${n === 1 ? "" : "s"}`;

/** Pretty JSON with keys, strings, numbers and literals marked for colour. */
function highlightJson(text: string): (Node | string)[] {
  const out: (Node | string)[] = [];
  const token = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  let last = 0;
  for (const match of text.matchAll(token)) {
    out.push(text.slice(last, match.index));
    if (match[1]) out.push(h("span", { class: match[2] ? "j-key" : "j-str" }, match[1]), match[2] ?? "");
    else if (match[3]) out.push(h("span", { class: "j-lit" }, match[3]));
    else out.push(h("span", { class: "j-num" }, match[4] ?? ""));
    last = (match.index ?? 0) + match[0].length;
  }
  out.push(text.slice(last));
  return out;
}

// ===== a small, safe markdown renderer =====

export function renderMarkdown(text: string): HTMLElement[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: HTMLElement[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    if (/^```/.test(line)) {
      const code: string[] = [];
      for (i += 1; i < lines.length && !/^```/.test(lines[i]!); i += 1) code.push(lines[i]!);
      i += 1;
      out.push(h("pre", { class: "mono" }, code.join("\n")));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      out.push(h(`h${Math.min(6, heading[1]!.length + 2)}` as "h3", {}, ...inline(heading[2]!)));
      i += 1;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push(h("hr", {}));
      i += 1;
      continue;
    }
    if (/^\|.+\|\s*$/.test(line) && /^\|?\s*:?-{3,}/.test(lines[i + 1] ?? "")) {
      const split = (row: string): string[] => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const header = split(line);
      const rows: string[][] = [];
      for (i += 2; i < lines.length && /^\|.+\|\s*$/.test(lines[i]!); i += 1) rows.push(split(lines[i]!));
      out.push(h("table", { class: "vv-table" }, h("thead", {}, h("tr", {}, ...header.map((c) => h("th", {}, ...inline(c))))), h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, ...inline(c))))))));
      continue;
    }
    const listItem = /^\s*([-*+]|\d+\.)\s+(.*)$/;
    if (listItem.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: HTMLElement[] = [];
      for (; i < lines.length && listItem.test(lines[i]!); i += 1) items.push(h("li", {}, ...inline(listItem.exec(lines[i]!)![2]!)));
      out.push(h(ordered ? "ol" : "ul", {}, ...items));
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quoted: string[] = [];
      for (; i < lines.length && /^>\s?/.test(lines[i]!); i += 1) quoted.push(lines[i]!.replace(/^>\s?/, ""));
      out.push(h("blockquote", {}, ...renderMarkdown(quoted.join("\n"))));
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const paragraph: string[] = [];
    for (; i < lines.length && lines[i]!.trim() && !/^(#{1,6}\s|```|\s*([-*+]|\d+\.)\s|>|\|)/.test(lines[i]!); i += 1) paragraph.push(lines[i]!);
    if (!paragraph.length) paragraph.push(lines[i++]!);
    out.push(h("p", {}, ...inline(paragraph.join(" "))));
  }
  return out;
}

function inline(text: string): (Node | string)[] {
  const out: (Node | string)[] = [];
  const token = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(token)) {
    out.push(text.slice(last, m.index));
    if (m[1] !== undefined) out.push(h("code", {}, m[1]));
    else if (m[2] !== undefined) out.push(h("strong", {}, m[2]));
    else if (m[3] !== undefined) out.push(h("em", {}, m[3]));
    else {
      const href = m[5] ?? "";
      out.push(/^(https?:|mailto:)/i.test(href) ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, m[4] ?? "") : (m[4] ?? ""));
    }
    last = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}
