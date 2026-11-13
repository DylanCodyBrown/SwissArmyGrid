/**
 * aiPanel.ts: the Sandbox: a dashboard of charts, notes, result tables, diagrams and images. [WIRE]
 *
 * It lives in the Sandbox tab: the first tab of the tab bar, always there, which shows this panel
 * full width (panes.ts). It is one sandbox for every table: each card names the table it is drawn on
 * (GET /cards), and Add chart picks the table. The grid tabs give the grids the full width.
 *
 * A dashboard: cards sit on a 12-column grid and each one is resized from its corner (or with the
 * arrow keys on that grip); what it draws fills it and redraws at the new size. Card kinds: Vega-Lite
 * charts (from the table), Markdown notes, SQL result tables, Mermaid and Graphviz diagrams, Plotly
 * and ECharts figures (JSON), and images (a picture from Python: matplotlib, seaborn and the like).
 * Every renderer but Vega-Lite and Markdown loads only when its first card draws.
 * The column profile is a popup now (profileView.openColumnProfile), and the agent's activity is
 * listed in Settings (agentAccess.ts).
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
import { api, grid, gridId, gridPath, type ColumnInfo, type GridInfo, type Notice } from "./api";
import { contextOf, gridList, onAgentView } from "./panes";
import { button, clear, formatCount, h, icon, isDateType, isNumericType, openDialog, recall, remember, toast } from "./dom";
import { renderMarkdown } from "./markdown";
import { saveBlob } from "./profileView";
import { state } from "./state";
import { sandboxMarkdown, svgToPng } from "./sandboxExport";
import { dashboardsDialog } from "./sandboxDashboards";
import { PAPERS, fitPages, makeMovable, moveCard, paginate, printPages, type Paper } from "./sandboxPages";
import { effectiveTheme, onThemeChange, setTheme, themeChoice } from "./theme";
import type { Drawn } from "./lazy/charts";

type CardKind = "chart" | "note" | "table" | "diagram" | "graphviz" | "plotly" | "echarts" | "vegalite" | "image";

interface Card {
  id: string;
  kind: CardKind;
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
  grid: string | null; // the grid the card is drawn on; null for a card of no table (Python, a note)
  dataset: string | null;
  origin?: string; // a card of no table: "Python", "Agent" or "Sandbox"
  size?: { w: number; h: number }; // columns of 12, rows of ROW_HEIGHT; else the kind's default
  new_page?: boolean; // in Page view (and the PDF), this card starts a new page
}

// ===== the dashboard grid =====

const COLUMNS = 12;
const ROW_HEIGHT = 40; // px per row unit (grid.css: .cards grid-auto-rows)
const GAP = 12;
const DEFAULT_SIZE: Record<CardKind, { w: number; h: number }> = {
  chart: { w: 6, h: 9 },
  note: { w: 4, h: 6 },
  table: { w: 6, h: 8 },
  diagram: { w: 6, h: 10 },
  graphviz: { w: 6, h: 10 },
  plotly: { w: 6, h: 10 },
  echarts: { w: 6, h: 10 },
  vegalite: { w: 6, h: 10 },
  image: { w: 6, h: 10 },
};
const KIND_NAMES: Record<CardKind, string> = {
  chart: "Chart",
  note: "Note",
  table: "Result",
  diagram: "Diagram",
  graphviz: "Graph",
  plotly: "Plotly figure",
  echarts: "ECharts chart",
  vegalite: "Vega-Lite chart",
  image: "Image",
};

/** A card's size: what the viewer last set (kept in this browser until dashboards are saved on the
 * server), else what the card carries, else its kind's default. */
function sizeOf(card: Card): { w: number; h: number } {
  const saved = recall(`sandbox.size.${card.id}`);
  if (saved) {
    const [w, h] = saved.split("x").map(Number);
    if (w && h) return clampSize(w, h);
  }
  return card.size ? clampSize(card.size.w, card.size.h) : DEFAULT_SIZE[card.kind];
}

function clampSize(w: number, h: number): { w: number; h: number } {
  return { w: Math.max(2, Math.min(COLUMNS, Math.round(w))), h: Math.max(3, Math.min(30, Math.round(h))) };
}

/** A card's place in its row. A card the viewer has not resized grows to fill its row, so a lone card
 * spans the width and a row never ends in a gap; a resized card keeps exactly the size it was given. */
function applySize(element: HTMLElement, size: { w: number; h: number }, fixed: boolean): void {
  const basis = `calc((100% - ${(COLUMNS - 1) * GAP}px) * ${size.w} / ${COLUMNS} + ${(size.w - 1) * GAP}px)`;
  element.style.flex = `${fixed ? 0 : 1} 1 ${basis}`;
  element.style.height = `${size.h * ROW_HEIGHT + (size.h - 1) * GAP}px`;
  element.dataset.w = String(size.w);
  element.dataset.h = String(size.h);
  element.classList.toggle("sized", fixed);
}

/** Whether the viewer (or the card itself) set this card's size. */
function hasOwnSize(card: Card): boolean {
  return Boolean(recall(`sandbox.size.${card.id}`) || card.size);
}

/** The corner grip: drag it (it snaps to the grid's columns and rows), or focus it and use the arrow
 * keys (left and right for width, up and down for height); a double-click returns the kind's size. */
function resizeGrip(card: Card, element: HTMLElement): HTMLElement {
  const grip = h("button", {
    type: "button",
    class: "card-grip",
    title: "Resize: drag, or use the arrow keys (double-click resets)",
    "aria-label": `Resize ${card.title || KIND_NAMES[card.kind]}: arrow keys change width and height`,
  });
  const set = (size: { w: number; h: number }, save: boolean, fixed = true): void => {
    applySize(element, size, fixed);
    grip.setAttribute("aria-valuetext", `${size.w} of ${COLUMNS} columns wide, ${size.h} rows tall`);
    if (save) {
      remember(`sandbox.size.${card.id}`, `${size.w}x${size.h}`);
      // Kept on the server too, so Page view, the PDF and another browser see the same board.
      void api(`/cards/${card.id}`, "PATCH", { size }).catch(() => undefined);
    }
  };
  set(sizeOf(card), false, hasOwnSize(card));
  grip.addEventListener("pointerdown", (down) => {
    down.preventDefault();
    grip.setPointerCapture(down.pointerId);
    const list = element.parentElement!;
    const column = (list.clientWidth - GAP * (COLUMNS - 1)) / COLUMNS;
    const box = element.getBoundingClientRect();
    element.classList.add("resizing");
    const move = (event: PointerEvent): void => {
      const w = (event.clientX - box.left + GAP) / (column + GAP);
      const hh = (event.clientY - box.top + GAP) / (ROW_HEIGHT + GAP);
      set(clampSize(w, hh), false);
    };
    const up = (): void => {
      element.classList.remove("resizing");
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      set(clampSize(Number(element.dataset.w), Number(element.dataset.h)), true);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
  });
  grip.addEventListener("keydown", (event) => {
    const now = { w: Number(element.dataset.w), h: Number(element.dataset.h) };
    const step: Record<string, [number, number]> = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowDown: [0, 1], ArrowUp: [0, -1] };
    const [dw, dh] = step[event.key] ?? [0, 0];
    if (!dw && !dh) return;
    event.preventDefault();
    set(clampSize(now.w + dw, now.h + dh), true);
  });
  grip.addEventListener("dblclick", () => {
    try {
      localStorage.removeItem(`swissarmygrid.sandbox.size.${card.id}`);
    } catch {
      // no storage: the size simply is not kept
    }
    set(card.size ? clampSize(card.size.w, card.size.h) : DEFAULT_SIZE[card.kind], false, Boolean(card.size));
  });
  return grip;
}

/** Draw into `host` now and again whenever its size changes (after the resizing settles). */
function fitDrawing(host: HTMLElement, draw: (width: number, height: number) => void | Promise<void>): void {
  let last = "";
  let timer = 0;
  const observer = new ResizeObserver(() => {
    if (!host.isConnected) {
      observer.disconnect();
      return;
    }
    const width = Math.round(host.clientWidth);
    const height = Math.round(host.clientHeight);
    const key = `${width}x${height}`;
    if (!width || !height || key === last) return;
    const first = last === "";
    last = key;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => void draw(width, height), first ? 0 : 150);
  });
  observer.observe(host);
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

let plotlyModule: Promise<typeof import("./lazy/plotly")> | null = null;
let echartsModule: Promise<typeof import("./lazy/echarts")> | null = null;
let graphvizModule: Promise<typeof import("./lazy/graphviz")> | null = null;
const plotly = (): Promise<typeof import("./lazy/plotly")> =>
  (plotlyModule ??= import(/* external */ "/static/build/lazy/plotly.js" as string) as Promise<typeof import("./lazy/plotly")>);
const echarts = (): Promise<typeof import("./lazy/echarts")> =>
  (echartsModule ??= import(/* external */ "/static/build/lazy/echarts.js" as string) as Promise<typeof import("./lazy/echarts")>);
const graphviz = (): Promise<typeof import("./lazy/graphviz")> =>
  (graphvizModule ??= import(/* external */ "/static/build/lazy/graphviz.js" as string) as Promise<typeof import("./lazy/graphviz")>);
// A picture from Python, inline: <img> never runs what an SVG carries and never loads from outside.
const IMAGE_DATA = /^data:image\/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/;

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
        mode === "collapsed" ? h("span", { class: "rail-label" }, "Sandbox") : null,
      ),
    ),
    body,
  );
  if (mode === "collapsed") return;
  // The panel's title, centered and large like a table's name, above what it holds.
  body.append(h("h1", { id: "ai-panel-title", class: "sandbox-title" }, "Sandbox"));
  await renderCards(body);
}

// ===== cards =====

let onBoard = 0; // how many cards the Sandbox shows (the Dashboards window warns before replacing them)

async function renderCards(body: HTMLElement): Promise<void> {
  body.append(
    h(
      "div",
      { class: "add-step", style: { marginTop: "0", marginBottom: "10px" } },
      button("Add chart", () => chartDialog(), { icon: "profile" }),
      button("Add note", () => noteDialog(), { icon: "edit" }),
      h("span", { class: "toolbar-gap" }),
      button(pageView() ? "Back to the board" : "Page view", () => setPageView(!pageView()), {
        icon: pageView() ? "columns" : "rows",
        pressed: pageView(),
        title: "See the Sandbox as letter or landscape pages, as it will print",
      }),
      pageView()
        ? h(
            "select",
            { "aria-label": "Paper", onchange: (event: Event) => setPaper((event.target as HTMLSelectElement).value as Paper) },
            ...(Object.keys(PAPERS) as Paper[]).map((key) => h("option", { value: key, selected: key === paper() }, PAPERS[key].label)),
          )
        : null,
      button("Print to PDF", () => void printSandbox(), { icon: "export", title: "Print the pages (choose Save as PDF in the print dialog)" }),
      button("Export markdown", () => void exportMarkdown(), { icon: "code", title: "The whole Sandbox as one markdown file, charts embedded as pictures: to keep, or to give an AI model" }),
      button("Dashboards…", () => dashboardsDialog(onBoard, () => void render()), { icon: "columns", title: "Save the Sandbox as a named dashboard, or open one you saved (with its history)" }),
    ),
  );
  let cards: Card[];
  try {
    cards = (await api<{ cards: Card[] }>("/cards")).cards;
    onBoard = cards.length;
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
        h("strong", {}, "Nothing in the Sandbox yet"),
        h("span", {}, "Add a chart or a note here, or ask the agent: it can add charts, notes, diagrams and figures through the SwissArmyGrid MCP tools. Resize any card from its corner."),
      ),
    );
    return;
  }
  const order = cards.map((c) => c.id);
  const elements = cards.map((card, i) => cardElement(card, order, i));
  if (pageView()) {
    const pages = paginate(cards.map((card, i) => ({ element: elements[i]!, size: sizeOf(card), newPage: Boolean(card.new_page) })), paper(), body);
    body.append(pages);
    requestAnimationFrame(() => fitPages(pages, body.clientWidth));
    return;
  }
  const list = h("div", { class: "cards" });
  body.append(list);
  list.append(...elements);
}

// ===== pages and printing =====

const pageView = (): boolean => recall("sandbox.pages") === "1";
const paper = (): Paper => (recall("sandbox.paper") === "landscape" ? "landscape" : "letter");

function setPageView(on: boolean): void {
  remember("sandbox.pages", on ? "1" : "0");
  void render();
}

function setPaper(next: Paper): void {
  remember("sandbox.paper", next);
  void render();
}

/** How each drawn card makes a picture of itself (for the markdown export), filled as cards draw. */
const pictures = new Map<string, () => Promise<string | null>>();

/** The whole Sandbox as one markdown file, every drawing a PNG; drawn on light paper like the PDF. */
async function exportMarkdown(): Promise<void> {
  const theme = themeChoice();
  const dark = effectiveTheme() !== "light";
  try {
    if (dark) setTheme("light");
    await render();
    await new Promise((resolve) => setTimeout(resolve, 900)); // charts settle before their pictures
    const { cards } = await api<{ cards: Card[] }>("/cards");
    const text = await sandboxMarkdown(cards, (id) => pictures.get(id)?.() ?? Promise.resolve(null), new Date());
    saveBlob(new Blob([text], { type: "text/markdown" }), `sandbox-${new Date().toISOString().slice(0, 10)}.md`);
    toast(`Exported ${cards.length} card${cards.length === 1 ? "" : "s"} as markdown.`);
  } catch (e) {
    toast((e as Error).message, "error");
  } finally {
    if (dark) {
      setTheme(theme);
      void render();
    }
  }
}

/** Page view, on light paper, then the print dialog (Save as PDF); the theme and view come back after. */
async function printSandbox(): Promise<void> {
  const wasPages = pageView();
  const theme = themeChoice();
  const dark = effectiveTheme() !== "light";
  await printPages(
    paper(),
    async () => {
      if (!wasPages) remember("sandbox.pages", "1");
      if (dark) setTheme("light");
      await render();
      // Charts draw as they come into view and settle; give them a moment before the dialog.
      await new Promise((resolve) => setTimeout(resolve, 900));
      document.querySelectorAll<HTMLElement>(".pages").forEach((p) => p.style.setProperty("zoom", "1"));
    },
    () => {
      if (dark) setTheme(theme);
      if (!wasPages) remember("sandbox.pages", "0");
      void render();
    },
  );
}

function cardElement(card: Card, order: string[], index: number): HTMLElement {
  const content = h("div", { class: "card-body" });
  const foot = h("div", { class: "card-foot" });
  let drawn: Drawn | null = null;
  pictures.set(card.id, async () => (drawn ? drawn.toPng() : null));
  const actions = h(
    "span",
    { class: "actions" },
    card.created_by === "agent" ? h("span", { class: "badge agent", title: "Added by the agent" }, icon("agent"), "Agent") : null,
    button(card.pinned ? "Unpin" : "Pin to the top", () => void api(`/cards/${card.id}`, "PATCH", { pinned: !card.pinned }).then(() => render()), { icon: "pin", iconOnly: true, class: "ghost", pressed: card.pinned }),
    card.kind === "chart" && !card.error ? button("Open large (zoom)", () => void expandChart(card), { icon: "zoom", iconOnly: true, class: "ghost" }) : null,
    button("More card actions", (event) => openMenu(event, [
      ...(card.kind === "note" ? [["edit", "Edit note", () => noteDialog(card)] as [string, string, () => void]] : []),
      ...(card.kind === "chart" && !card.error
        ? ([
            ["image", "Download image (PNG)", () => void drawn?.toPng().then((url) => { const a = h("a", { href: url, download: `${card.title || "chart"}.png` }); a.click(); })],
            ["export", "Download as HTML", () => void exportHtml(card, drawn)],
            ["code", "Copy as Python (Altair)", () => void copyAltair(card)],
          ] as [string, string, () => void][])
        : []),
      ...(index > 0 ? [["up", "Move earlier", () => void moveCard(card.id, order[index - 1]!).then(() => render())] as [string, string, () => void]] : []),
      ...(index < order.length - 1 ? [["down", "Move later", () => void moveCard(card.id, order[index + 2] ?? null).then(() => render())] as [string, string, () => void]] : []),
      ["rows", card.new_page ? "Don't start a new page" : "Start a new page here", () => void api(`/cards/${card.id}`, "PATCH", { new_page: !card.new_page }).then(() => render())],
    ]), { icon: "more", iconOnly: true, class: "ghost" }),
    h("span", { class: "danger-gap" }, button("Remove", () => void api(`/cards/${card.id}`, "DELETE").then(() => render()), { icon: "trash", iconOnly: true, class: "ghost" })),
  );
  const header = h("div", { class: "card-header" }, h("h3", {}, card.title || (card.kind === "chart" ? chartTitle(card) : KIND_NAMES[card.kind])), h("span", { class: "badge card-table", title: card.dataset ? `Drawn on ${card.dataset}` : `From ${card.origin ?? "the Sandbox"}: belongs to no table` }, card.dataset ?? card.origin ?? "Sandbox"), actions);
  const element = h(
    "article",
    { class: `card card-${card.kind} ${card.pinned ? "pinned" : ""} ${card.new_page ? "new-page" : ""}`, "aria-label": card.title || KIND_NAMES[card.kind] },
    header,
    content,
    foot,
  );
  element.append(resizeGrip(card, element));
  makeMovable(element, header, card.id, order[index + 1] ?? null, () => void render());
  const dark = effectiveTheme() !== "light";
  const failed = (slot: HTMLElement, e: unknown): void => {
    slot.textContent = `This could not be drawn: ${(e as Error).message}`;
    slot.classList.add("error-text");
  };
  if (card.error) {
    content.append(h("p", { class: "error-text" }, `⚠ ${card.error}`));
  } else if (card.kind === "note") {
    content.append(renderMarkdown(card.body ?? ""));
    content.addEventListener("dblclick", () => noteDialog(card));
    content.title = "Double-click to edit";
  } else if (card.kind === "diagram") {
    // Mermaid text (an ERD from the relationship model), drawn by the lazy Mermaid chunk.
    const slot = h("div", { class: "diagram", role: "img", "aria-label": card.title || "Diagram" });
    content.append(slot);
    pictures.set(card.id, async () => { const svg = slot.querySelector("svg"); return svg ? svgToPng(svg) : null; });
    void diagrams().then((m) => m.drawDiagram(slot, card.body ?? "", dark));
  } else if (card.kind === "graphviz") {
    const slot = h("div", { class: "diagram", role: "img", "aria-label": card.title || "Graph" });
    content.append(slot);
    pictures.set(card.id, async () => { const svg = slot.querySelector("svg"); return svg ? svgToPng(svg) : null; });
    void graphviz().then((m) => m.drawGraphviz(slot, card.body ?? "", dark)).catch((e: unknown) => failed(slot, e));
  } else if (card.kind === "plotly" || card.kind === "echarts") {
    const slot = h("div", { class: "figure", role: "img", "aria-label": card.title || KIND_NAMES[card.kind] });
    content.append(slot);
    let drawnFigure: { resize: () => void; toPng: () => string | Promise<string> } | null = null;
    pictures.set(card.id, async () => (drawnFigure ? drawnFigure.toPng() : null));
    fitDrawing(slot, async () => {
      try {
        if (drawnFigure) return drawnFigure.resize();
        drawnFigure = card.kind === "plotly" ? await (await plotly()).drawPlotly(slot, card.body ?? "", dark) : (await echarts()).drawECharts(slot, card.body ?? "", dark);
      } catch (e) {
        failed(slot, e);
      }
    });
  } else if (card.kind === "vegalite") {
    const slot = h("div", { class: "chart", role: "img", "aria-label": card.title || "Vega-Lite chart" });
    content.append(slot);
    let own: Drawn | null = null;
    pictures.set(card.id, async () => (own ? own.toPng() : null));
    fitDrawing(slot, async (width, height) => {
      try {
        if (own) {
          await own.result.view.width(Math.max(120, width - 40)).height(Math.max(120, height - 40)).runAsync();
          return;
        }
        own = await (await charts()).drawChart(slot, JSON.parse(card.body ?? "{}") as Record<string, unknown>, { own: true, height: Math.max(120, height - 40) });
      } catch (e) {
        failed(slot, e);
      }
    });
  } else if (card.kind === "image") {
    const source = card.body ?? "";
    pictures.set(card.id, async () => (IMAGE_DATA.test(source) ? source : null));
    if (IMAGE_DATA.test(source)) content.append(h("img", { class: "card-image", src: source, alt: card.title || "Image" }));
    else content.append(h("p", { class: "error-text" }, "This image is not an inline PNG, JPEG, GIF, WebP or SVG picture."));
  } else if (card.kind === "table") {
    content.append(smallTable(card.columns ?? [], card.rows ?? []));
    if (card.capped) foot.textContent = "Showing the first 100 rows.";
  } else if (card.vega_lite) {
    // A histogram's bar is a range, which the column profile filters on; every other mark is one value.
    const clickable = markOf(card) !== "histogram";
    const chart = h("div", { class: "chart", role: "img", "aria-label": `${chartTitle(card)}: ${card.points ?? 0} points.${clickable ? " Click a mark to filter the grid." : ""}` });
    content.append(chart);
    foot.textContent = [card.note, `${formatCount(card.points ?? 0)} points${clickable ? " · click a mark to filter" : ""}`].filter(Boolean).join(" · ");
    // The chart fills its card. Resized, it changes size in place (its marks stay the same elements,
    // so a click in the meantime still lands); it is drawn afresh only the first time.
    fitDrawing(chart, async (width, height) => {
      if (drawn) {
        await drawn.result.view.width(width).height(Math.max(120, height - 40)).runAsync();
        return;
      }
      const m = await charts();
      drawn = await m.drawChart(chart, card.vega_lite!, {
        onSelect: (value, y2) => void crossFilter(card, value, y2),
        height: Math.max(120, height - 40), // room for the axis labels under the plot
        fontScale: panelZoom,
        large: workspace.dataset.panel === "max",
      });
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
      // A long value is cut to the cell's width (grid.css), the whole of it on hover.
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((v) => h("td", { class: typeof v === "number" ? "num" : "", title: v === null ? "" : String(v) }, v === null ? "(blank)" : String(v)))))),
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
    const owner = (card.grid ? contextOf(card.grid)?.state : undefined) ?? state;
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
  const page = `<!doctype html><meta charset="utf-8"><title>${title.replace(/</g, "&lt;")}</title><body style="font-family:Segoe UI,sans-serif;background:#fff;margin:24px">${svg}<p style="color:#57606a">From SwissArmyGrid, dataset ${(card.dataset ?? "").replace(/</g, "&lt;")}, ${new Date().toISOString()}</p></body>`;
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
  // Which table to chart: any open table, the one in front first. Only tables whose tab had been
  // opened were offered (their columns were in the page); the others' columns come from the server.
  const tables = gridList();
  const table = h("select", { "aria-label": "Table" });
  for (const g of tables) table.append(h("option", { value: g.id, selected: g.id === gridId }, g.label));
  let columns: ColumnInfo[] = [];
  const mark = h("select", { "aria-label": "Chart type" });
  for (const [v, label] of [["bar", "Bar"], ["line", "Line"], ["area", "Area"], ["point", "Scatter"], ["histogram", "Histogram"], ["heatmap", "Heatmap (counts)"]]) mark.append(h("option", { value: v }, label));
  const x = h("select", { "aria-label": "X axis" });
  const y = h("select", { "aria-label": "Y axis" });
  const color = h("select", { "aria-label": "Series" });
  const agg = h("select", { "aria-label": "Aggregate" });
  const unit = h("select", { "aria-label": "Time unit" });
  const title = h("input", { type: "text", "aria-label": "Title", placeholder: "optional" });
  const fill = async (): Promise<void> => {
    const id = table.value;
    const known = contextOf(id)?.state.info?.columns;
    const found = known ?? (await api<GridInfo>(gridPath(id)).then((info) => info.columns, () => [] as ColumnInfo[]));
    if (table.value !== id) return; // another table was picked meanwhile
    columns = found;
    clear(x, ...columns.map((c) => h("option", { value: c.name }, c.name)));
    clear(y, ...columns.map((c) => h("option", { value: c.name }, c.name)));
    clear(color, h("option", { value: "" }, "(none)"), ...columns.map((c) => h("option", { value: c.name }, c.name)));
    const numeric = columns.find((c) => isNumericType(c.type));
    if (numeric) y.value = numeric.name;
    sync();
  };
  for (const a of ["count", "sum", "mean", "min", "max", "(none: raw points)"]) agg.append(h("option", { value: a.startsWith("(") ? "" : a }, a));
  unit.append(h("option", { value: "" }, "(as is)"), ...["day", "week", "month", "quarter", "year"].map((u) => h("option", { value: u }, u)));
  const field = (label: string, el: HTMLElement): HTMLElement => h("label", { class: "field" }, h("span", {}, label), el);
  const unitField = field("Time unit", unit);
  const sync = (): void => {
    unitField.hidden = !isDateType(columns.find((c) => c.name === x.value)?.type ?? "");
    y.disabled = mark.value === "histogram" || (agg.value === "count" && mark.value !== "heatmap");
  };
  for (const el of [mark, x, agg]) el.addEventListener("change", sync);
  table.addEventListener("change", () => void fill());
  sync();
  void fill();
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

/** Add a note, or edit one (an analysis's note too: add your own words to it). */
function noteDialog(existing?: Card): void {
  const title = h("input", { type: "text", "aria-label": "Title", placeholder: "optional", value: existing?.title ?? "" });
  const text = h("textarea", { rows: "14", "aria-label": "Note (markdown)", placeholder: "# Headings, **bold**, *italic*, `code`, - lists, > cautions, and tables:\n| Region | Late |\n|---|---:|\n| West | 46% |" });
  text.value = existing?.body ?? "";
  const { body, foot, close } = openDialog(existing ? "Edit the note" : "Add a note", true);
  body.append(
    h("label", { class: "field" }, h("span", {}, "Title"), title),
    h("label", { class: "field" }, h("span", {}, "Note (markdown)"), text),
    h("p", { class: "hint" }, existing ? "Changes show for everyone looking at the Sandbox." : `The note is filed with ${state.info?.dataset ?? "the table in front"}.`),
  );
  const save = (): Promise<unknown> =>
    existing ? api(`/cards/${existing.id}`, "PATCH", { title: title.value, body: text.value }) : api("/cards", "POST", { kind: "note", title: title.value, body: text.value });
  foot.append(button("Cancel", close), button(existing ? "Save" : "Add note", () => void save().then(() => { close(); void render(); }).catch((e: Error) => toast(e.message, "error")), { class: "primary" }));
}

export function refreshPanel(): void {
  void render();
}

// Used by tests and the toolbar to read the theme Monaco and charts follow.
export const currentTheme = effectiveTheme;
