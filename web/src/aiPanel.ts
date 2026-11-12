/**
 * aiPanel.ts: the Analysis Sandbox: the charts, notes and result tables beside a table. [WIRE]
 *
 * It lives in the Agent tab only: the first tab of the tab bar, always there, which shows this
 * panel full width (panes.ts). It is one sandbox for every table: each card names the table it is
 * drawn on (GET /cards), and Add chart picks the table. The grid tabs give the grids the full width.
 * The column profile is a popup now (profileView.openColumnProfile), and what the agent may see
 * is set in Settings for every table (agentAccess.ts).
 *
 * Split-panel styles: the panel sits to the right of the grid or below it, its size drags
 * (or moves with the arrow keys on the splitter), it collapses to a slim rail or maximizes
 * over the grid. Zooming: the panel has its own zoom level, and any chart opens large with
 * wheel zoom and drag-to-pan. Layout, size and zoom are remembered per viewer.
 *
 * ROUTING: [SPLITTER]        drag / arrows / double-click -> setSize()
 *          [LAYOUT BUTTON]   -> setLayout("right" | "bottom")
 *          [COLLAPSE / MAX]  -> setMode("open" | "collapsed" | "max")
 *          [PANEL ZOOM]      -> setPanelZoom()
 *          [CARDS]           from GET /cards (every table); [ADD CHART]/[ADD NOTE] -> chartDialog() / noteDialog()
 *          [CARD ACTIONS]    pin / expand (zoom) / image / HTML / Altair / remove
 *          [CARD TABLE]      each card's table name -> read-only, from the card's grid
 */
import { api, grid, gridId, type Notice } from "./api";
import { contextOf, gridList, onAgentView, viewingAgent } from "./panes";
import { button, clear, formatCount, h, icon, isDateType, isNumericType, openDialog, recall, remember, toast } from "./dom";
import { renderMarkdown } from "./markdown";
import { saveBlob } from "./profileView";
import { state } from "./state";
import { effectiveTheme, onThemeChange } from "./theme";
import type { Drawn } from "./lazy/charts";

interface Card {
  id: string;
  kind: "chart" | "note" | "table" | "diagram";
  title: string;
  spec?: Record<string, unknown>;
  body?: string;
  sql?: string;
  vega_lite?: Record<string, unknown>;
  note?: string | null;
  points?: number;
  columns?: string[];
  rows?: unknown[][];
  capped?: boolean;
  error?: string;
  pinned: boolean;
  created_by: string;
  grid: string; // the grid the card is drawn on (the sandbox shows every table's cards)
  dataset: string;
}

let workspace: HTMLElement;
let panel: HTMLElement;
let panelZoom = Number(recall("panel.zoom") ?? "1") || 1;
let chartsModule: Promise<typeof import("./lazy/charts")> | null = null;

let diagramModule: Promise<typeof import("./lazy/diagram")> | null = null;
const diagrams = (): Promise<typeof import("./lazy/diagram")> => {
  diagramModule ??= import(/* external */ "/static/build/lazy/diagram.js" as string) as Promise<typeof import("./lazy/diagram")>;
  return diagramModule;
};

const charts = (): Promise<typeof import("./lazy/charts")> => {
  chartsModule ??= import(/* external */ "/static/build/lazy/charts.js" as string) as Promise<typeof import("./lazy/charts")>;
  return chartsModule;
};

// ===== split layout =====

export function mountAiPanel(space: HTMLElement, host: HTMLElement, splitter: HTMLElement): void {
  onAgentView(() => void render());
  // Never beside the grids: the panel shows only in the Agent tab.
  space.dataset.panel = "detached";
  workspace = space;
  panel = host;
  setLayout((recall("panel.layout") as "right" | "bottom" | null) ?? "right", false);
  // Narrow windows (VS Code's side browser, a laptop split) start with the panel collapsed.
  const saved = Number(recall(`panel.size.${workspace.dataset.layout}`));
  if (saved) setSize(saved, false);
  setPanelZoom(panelZoom, false);

  splitter.setAttribute("role", "separator");
  splitter.setAttribute("tabindex", "0");
  splitter.setAttribute("aria-label", "Resize the analysis panel (arrow keys; double-click resets)");
  splitter.addEventListener("pointerdown", (down) => {
    if (workspace.dataset.panel !== "open") return;
    splitter.setPointerCapture(down.pointerId);
    splitter.classList.add("dragging");
    const rect = workspace.getBoundingClientRect();
    const move = (event: PointerEvent): void => {
      const size = workspace.dataset.layout === "bottom" ? rect.bottom - event.clientY : rect.right - event.clientX;
      setSize(size, false);
    };
    const up = (): void => {
      splitter.classList.remove("dragging");
      splitter.removeEventListener("pointermove", move);
      splitter.removeEventListener("pointerup", up);
      setSize(currentSize(), true);
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", up);
  });
  splitter.addEventListener("keydown", (event) => {
    const grow = workspace.dataset.layout === "bottom" ? ["ArrowUp"] : ["ArrowLeft"];
    const shrink = workspace.dataset.layout === "bottom" ? ["ArrowDown"] : ["ArrowRight"];
    if (grow.includes(event.key)) setSize(currentSize() + 24, true);
    else if (shrink.includes(event.key)) setSize(currentSize() - 24, true);
    else return;
    event.preventDefault();
  });
  splitter.addEventListener("dblclick", () => setSize(workspace.dataset.layout === "bottom" ? 320 : 400, true));

  state.on("info", () => void render());
  onThemeChange(() => void render());
}

function currentSize(): number {
  return parseInt(getComputedStyle(workspace).getPropertyValue("--panel-size"), 10) || 380;
}

function setSize(size: number, save: boolean): void {
  const rect = workspace.getBoundingClientRect();
  const limit = workspace.dataset.layout === "bottom" ? rect.height - 120 : rect.width - 280;
  const clamped = Math.round(Math.max(220, Math.min(Math.max(240, limit), size)));
  workspace.style.setProperty("--panel-size", `${clamped}px`);
  if (save) remember(`panel.size.${workspace.dataset.layout}`, String(clamped));
}

export function setLayout(layout: "right" | "bottom", save = true): void {
  workspace.dataset.layout = layout;
  workspace.style.removeProperty("--panel-size");
  const saved = Number(recall(`panel.size.${layout}`));
  if (saved) setSize(saved, false);
  if (save) {
    remember("panel.layout", layout);
    void render();
  }
}

export function setMode(mode: "open" | "collapsed" | "max", save = true): void {
  workspace.dataset.panel = mode;
  state.emit("selection"); // the status bar words itself for a hidden grid
  if (save) {
    remember("panel.mode", mode);
    void render();
  }
}

function setPanelZoom(z: number, save = true): void {
  panelZoom = Math.min(1.8, Math.max(0.7, Math.round(z * 10) / 10));
  workspace.style.setProperty("--panel-zoom", String(panelZoom));
  if (save) {
    remember("panel.zoom", String(panelZoom));
    void render();
  }
}


// ===== live notices =====

export function handleNotice(notice: Notice): void {
  // One sandbox for every table: any table's cards changing redraws it.
  if (notice.type === "cards_updated") void render();
}

// ===== render =====

async function render(): Promise<void> {
  if (!panel || !state.info) return;
  // Hidden (the grids are showing): nothing to draw. Every step change and tab switch used to
  // fetch the cards and redraw every chart here unseen; showing the Agent tab draws it fresh.
  if (!panel.getClientRects().length) return;
  const mode = workspace.dataset.panel;
  const layout = workspace.dataset.layout;
  const body = h("div", { class: "panel-body", id: "ai-panel-body" });
  clear(
    panel,
    h(
      "div",
      { class: "panel-header" },
      // The table picker first, at the far left, where it is easy to find.
      h("span", { class: "panel-header-gap" }),
      h(
        "span",
        { class: "zoom-control", role: "group", "aria-label": "Panel zoom" },
        button("Zoom out the panel", () => setPanelZoom(panelZoom - 0.1), { icon: "minus", iconOnly: true }),
        h("output", { "aria-live": "polite" }, `${Math.round(panelZoom * 100)}%`),
        button("Zoom in the panel", () => setPanelZoom(panelZoom + 0.1), { icon: "plus", iconOnly: true }),
      ),
      button(layout === "bottom" ? "Dock the panel to the right" : "Dock the panel below the grid", () => setLayout(layout === "bottom" ? "right" : "bottom"), { icon: layout === "bottom" ? "right" : "bottom", iconOnly: true, class: "ghost" }),
      button(mode === "max" ? "Restore the panel" : "Maximize the panel", () => setMode(mode === "max" ? "open" : "max"), { icon: mode === "max" ? "restore" : "max", iconOnly: true, class: "ghost" }),
      h(
        "span",
        { class: "collapse-toggle" },
        button(mode === "collapsed" ? "Open the analysis panel" : "Collapse the panel", () => setMode(mode === "collapsed" ? "open" : "collapsed"), {
          icon: mode === "collapsed" ? (layout === "bottom" ? "up" : "collapse") : layout === "bottom" ? "down" : "expand",
          iconOnly: true,
          class: "ghost",
        }),
        mode === "collapsed" ? h("span", { class: "rail-label" }, "Analysis Sandbox") : null,
      ),
    ),
    body,
  );
  if (mode === "collapsed") return;
  // The panel's title, centered and large like a table's name, above what it holds.
  body.append(h("h1", { id: "ai-panel-title", class: "sandbox-title" }, "Analysis Sandbox"));
  await renderCards(body);
}

// ===== cards =====

async function renderCards(body: HTMLElement): Promise<void> {
  body.append(
    h(
      "div",
      { class: "add-step", style: { marginTop: "0", marginBottom: "10px" } },
      button("Add chart", () => chartDialog(), { icon: "profile" }),
      button("Add note", () => noteDialog(), { icon: "edit" }),
    ),
  );
  let cards: Card[];
  try {
    cards = (await api<{ cards: Card[] }>("/cards")).cards;
  } catch (e) {
    body.append(h("p", { class: "status-error" }, (e as Error).message));
    return;
  }
  if (!cards.length) {
    body.append(
      h(
        "div",
        { class: "empty-state" },
        icon("agent"),
        h("strong", {}, "No charts or notes yet"),
        h("span", {}, "Add one here, or ask the agent: it can add charts and notes beside this grid through the SwissArmyGrid MCP tools."),
      ),
    );
    return;
  }
  const list = h("div", { class: "cards" });
  body.append(list);
  for (const card of cards) list.append(cardElement(card));
}

function cardElement(card: Card): HTMLElement {
  const content = h("div", { class: "card-body" });
  const foot = h("div", { class: "card-foot" });
  let drawn: Drawn | null = null;
  const actions = h(
    "span",
    { class: "actions" },
    card.created_by === "agent" ? h("span", { class: "badge agent", title: "Added by the agent" }, icon("agent"), "Agent") : null,
    button(card.pinned ? "Unpin" : "Pin to the top", () => void api(`/cards/${card.id}`, "PATCH", { pinned: !card.pinned }).then(() => render()), { icon: "pin", iconOnly: true, class: "ghost", pressed: card.pinned }),
    card.kind === "chart" && !card.error ? button("Open large (zoom)", () => void expandChart(card), { icon: "zoom", iconOnly: true, class: "ghost" }) : null,
    card.kind === "chart" && !card.error
      ? button("More chart actions", (event) => openMenu(event, [
          ["image", "Download image (PNG)", () => void drawn?.toPng().then((url) => { const a = h("a", { href: url, download: `${card.title || "chart"}.png` }); a.click(); })],
          ["export", "Download as HTML", () => void exportHtml(card, drawn)],
          ["code", "Copy as Python (Altair)", () => void copyAltair(card)],
        ]), { icon: "more", iconOnly: true, class: "ghost" })
      : null,
    h("span", { class: "danger-gap" }, button("Remove", () => void api(`/cards/${card.id}`, "DELETE").then(() => render()), { icon: "trash", iconOnly: true, class: "ghost" })),
  );
  const element = h(
    "article",
    { class: `card ${card.pinned ? "pinned" : ""}`, "aria-label": card.title || card.kind },
    h("div", { class: "card-header" }, h("h3", {}, card.title || (card.kind === "note" ? "Note" : card.kind === "table" ? "Result" : card.kind === "diagram" ? "Diagram" : chartTitle(card))), h("span", { class: "badge card-table", title: `Drawn on ${card.dataset}` }, card.dataset), actions),
    content,
    foot,
  );
  if (card.error) {
    content.append(h("p", { class: "error-text" }, `⚠ ${card.error}`));
  } else if (card.kind === "note") {
    content.append(renderMarkdown(card.body ?? ""));
  } else if (card.kind === "diagram") {
    // Mermaid text (an ERD from the relationship model), drawn by the lazy Mermaid chunk.
    const slot = h("div", { class: "diagram", role: "img", "aria-label": card.title || "Diagram" });
    content.append(slot);
    void diagrams().then((m) => m.drawDiagram(slot, card.body ?? "", effectiveTheme() !== "light"));
  } else if (card.kind === "table") {
    content.append(smallTable(card.columns ?? [], card.rows ?? []));
    if (card.capped) foot.textContent = "Showing the first 100 rows.";
  } else if (card.vega_lite) {
    // A histogram's bar is a range, which the column profile filters on; every other mark is one value.
    const clickable = markOf(card) !== "histogram";
    const chart = h("div", { class: "chart", role: "img", "aria-label": `${chartTitle(card)}: ${card.points ?? 0} points.${clickable ? " Click a mark to filter the grid." : ""}` });
    content.append(chart);
    foot.textContent = [card.note, `${formatCount(card.points ?? 0)} points${clickable ? " · click a mark to filter" : ""}`].filter(Boolean).join(" · ");
    void charts().then(async (m) => {
      const max = workspace.dataset.panel === "max";
      const roomy = max || viewingAgent(); // the Agent tab gives each chart a full-width, tall card
      const room = workspace.getBoundingClientRect().height;
      const maxHeight = Math.min(480, Math.max(320, Math.round(room * (workspace.dataset.layout === "bottom" ? 0.55 : 0.4))));
      const height = Math.round((roomy ? maxHeight : workspace.dataset.layout === "bottom" ? 150 : 280) * (roomy ? 1 : panelZoom));
      drawn = await m.drawChart(chart, card.vega_lite!, { onSelect: (value, y2) => void crossFilter(card, value, y2), height, fontScale: panelZoom, large: max });
    });
  }
  return element;
}

function openMenu(event: MouseEvent, items: [string, string, () => void][]): void {
  document.querySelector(".menu")?.remove();
  const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect();
  const menu = h("div", { class: "menu", role: "menu" }, ...items.map(([name, label, run]) => h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); run(); } }, icon(name), label)));
  document.body.append(menu);
  menu.style.top = `${anchor.bottom + 4}px`;
  menu.style.left = `${Math.max(8, anchor.right - menu.offsetWidth)}px`;
  (menu.querySelector("button") as HTMLButtonElement | null)?.focus();
  setTimeout(() => document.addEventListener("click", function away(e) {
    if (!menu.contains(e.target as Node)) { menu.remove(); document.removeEventListener("click", away); }
  }), 0);
  menu.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.remove(); });
}

function chartTitle(card: Card): string {
  const spec = card.spec as { mark?: string; x?: { field?: string }; y?: { field?: string; aggregate?: string } } | undefined;
  if (!spec) return "Chart";
  const y = spec.y?.aggregate === "count" ? "rows" : `${spec.y?.aggregate ?? ""} ${spec.y?.field ?? ""}`.trim();
  return spec.mark === "histogram" ? `Distribution of ${spec.x?.field}` : `${y || "value"} by ${spec.x?.field}`;
}

function smallTable(columns: string[], rows: unknown[][]): HTMLElement {
  return h(
    "div",
    { style: { overflow: "auto", maxHeight: "320px" } },
    h(
      "table",
      { class: "small" },
      h("thead", {}, h("tr", {}, ...columns.map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((v) => h("td", { class: typeof v === "number" ? "num" : "" }, v === null ? "(blank)" : String(v)))))),
    ),
  );
}

function markOf(card: Card): string {
  return (card.spec as { mark?: string } | undefined)?.mark ?? "";
}

async function crossFilter(card: Card, value: unknown, y2?: unknown): Promise<void> {
  const spec = card.spec as { x?: { field?: string }; y?: { field?: string } } | undefined;
  const shown = (v: unknown): string => (v === null ? "(blank)" : String(v));
  // A heatmap cell is a pair: its row's value filters too.
  const cell = markOf(card) === "heatmap" && y2 !== undefined;
  try {
    await api(`/cards/${card.id}/select`, "POST", cell ? { value, y: y2 } : { value });
    toast(`Filtered the grid to ${String(spec?.x?.field)} = ${shown(value)}${cell ? ` and ${String(spec?.y?.field)} = ${shown(y2)}` : ""}.`);
    // The card's own table gets the filter step, whichever table is in front.
    const owner = contextOf(card.grid)?.state ?? state;
    // A new generation before load() redraws the panels, so their requests are not stale (409).
    owner.gen += 1;
    await owner.load();
    owner.emit("view");
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function expandChart(card: Card): Promise<void> {
  const { body, foot, close } = openDialog(card.title || chartTitle(card), true);
  const chart = h("div", { class: "expanded-chart" });
  const mark = markOf(card);
  const zoomable = ["point", "line", "area"].includes(mark);
  const hint = zoomable
    ? "Scroll to zoom and drag to pan; double-click resets. Click a point to filter the grid."
    : mark === "histogram"
      ? "Hover a bar for its range and count; the column profile filters on a range."
      : mark === "heatmap"
        ? "Click a cell to filter the grid to its pair of values."
        : "Click a bar to filter the grid.";
  body.append(h("p", { class: "hint" }, hint), chart);
  foot.append(button("Close", close, { class: "primary" }));
  const m = await charts();
  await m.drawChart(chart, card.vega_lite!, { zoomable, large: true, height: Math.round(window.innerHeight * 0.6), onSelect: (v, y2) => { close(); void crossFilter(card, v, y2); } });
}

async function exportHtml(card: Card, drawn: Drawn | null): Promise<void> {
  if (!drawn) return;
  const svg = await drawn.toSvg();
  const title = card.title || chartTitle(card);
  const page = `<!doctype html><meta charset="utf-8"><title>${title.replace(/</g, "&lt;")}</title><body style="font-family:Segoe UI,sans-serif;background:#fff;margin:24px">${svg}<p style="color:#57606a">From SwissArmyGrid, dataset ${card.dataset.replace(/</g, "&lt;")}, ${new Date().toISOString()}</p></body>`;
  saveBlob(new Blob([page], { type: "text/html" }), `${title.replace(/[^A-Za-z0-9_-]+/g, "_") || "chart"}.html`);
}

async function copyAltair(card: Card): Promise<void> {
  try {
    const { code } = await api<{ code: string }>(`/cards/${card.id}/code`);
    await navigator.clipboard.writeText(code);
    toast("Copied the Altair code.");
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

function chartDialog(): void {
  // Which table to chart: any open table, the one in front first.
  const tables = gridList().filter((g) => contextOf(g.id)?.state.info);
  const table = h("select", { "aria-label": "Table" });
  for (const g of tables) table.append(h("option", { value: g.id, selected: g.id === gridId }, g.label));
  let columns = contextOf(table.value)?.state.info?.columns ?? state.info?.columns ?? [];
  const mark = h("select", { "aria-label": "Chart type" });
  for (const [v, label] of [["bar", "Bar"], ["line", "Line"], ["area", "Area"], ["point", "Scatter"], ["histogram", "Histogram"], ["heatmap", "Heatmap (counts)"]]) mark.append(h("option", { value: v }, label));
  const x = h("select", { "aria-label": "X axis" });
  const y = h("select", { "aria-label": "Y axis" });
  const color = h("select", { "aria-label": "Series" });
  const agg = h("select", { "aria-label": "Aggregate" });
  const unit = h("select", { "aria-label": "Time unit" });
  const title = h("input", { type: "text", "aria-label": "Title", placeholder: "optional" });
  const fill = (): void => {
    columns = contextOf(table.value)?.state.info?.columns ?? [];
    clear(x, ...columns.map((c) => h("option", { value: c.name }, c.name)));
    clear(y, ...columns.map((c) => h("option", { value: c.name }, c.name)));
    clear(color, h("option", { value: "" }, "(none)"), ...columns.map((c) => h("option", { value: c.name }, c.name)));
    const numeric = columns.find((c) => isNumericType(c.type));
    if (numeric) y.value = numeric.name;
  };
  fill();
  for (const a of ["count", "sum", "mean", "min", "max", "(none: raw points)"]) agg.append(h("option", { value: a.startsWith("(") ? "" : a }, a));
  unit.append(h("option", { value: "" }, "(as is)"), ...["day", "week", "month", "quarter", "year"].map((u) => h("option", { value: u }, u)));
  const field = (label: string, el: HTMLElement): HTMLElement => h("label", { class: "field" }, h("span", {}, label), el);
  const unitField = field("Time unit", unit);
  const sync = (): void => {
    unitField.hidden = !isDateType(columns.find((c) => c.name === x.value)?.type ?? "");
    y.disabled = mark.value === "histogram" || (agg.value === "count" && mark.value !== "heatmap");
  };
  for (const el of [mark, x, agg]) el.addEventListener("change", sync);
  table.addEventListener("change", () => { fill(); sync(); });
  sync();
  const { body, foot, close } = openDialog("Add a chart");
  const error = h("p", { class: "status-error", role: "alert" });
  body.append(
    h("div", { class: "form-grid" }, field("Table", table), field("Type", mark), field("X axis", x), unitField, field("Aggregate", agg), field("Y axis", y), field("Series (color + shape)", color), field("Title", title)),
    h("p", { class: "hint" }, "The server aggregates before drawing, so a chart never sends more than 5,000 points to the browser."),
    error,
  );
  foot.append(
    button("Cancel", close),
    button("Add chart", () => {
      const spec: Record<string, unknown> = { mark: mark.value, x: { field: x.value, ...(unit.value && !unitField.hidden ? { time_unit: unit.value } : {}) } };
      if (mark.value === "heatmap") spec.y = { field: y.value, aggregate: "count" };
      else if (mark.value !== "histogram") spec.y = agg.value === "count" ? { aggregate: "count" } : agg.value ? { field: y.value, aggregate: agg.value } : { field: y.value };
      if (color.value && mark.value !== "heatmap" && mark.value !== "histogram") spec.color = { field: color.value };
      void api(`/grids/${encodeURIComponent(table.value)}/cards`, "POST", { kind: "chart", title: title.value, spec })
        .then(() => { close(); void render(); })
        .catch((e: Error) => { error.textContent = e.message; });
    }, { class: "primary" }),
  );
}

function noteDialog(): void {
  const title = h("input", { type: "text", "aria-label": "Title", placeholder: "optional" });
  const text = h("textarea", { rows: "8", "aria-label": "Note (markdown)", placeholder: "**Bold**, *italic*, `code`, - lists, # headings" });
  const { body, foot, close } = openDialog("Add a note");
  body.append(h("label", { class: "field" }, h("span", {}, "Title"), title), h("label", { class: "field" }, h("span", {}, "Note (markdown)"), text), h("p", { class: "hint" }, `The note is filed with ${state.info?.dataset ?? "the table in front"}.`));
  foot.append(button("Cancel", close), button("Add note", () => void api(`${grid}/cards`, "POST", { kind: "note", title: title.value, body: text.value }).then(() => { close(); void render(); }).catch((e: Error) => toast(e.message, "error")), { class: "primary" }));
}

export function refreshPanel(): void {
  void render();
}

// Used by tests and the toolbar to read the theme Monaco and charts follow.
export const currentTheme = effectiveTheme;
