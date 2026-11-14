/**
 * sandboxPages.ts: the Sandbox as pages, and moving cards (Plans/sandbox-report.plan.md, S2). [SHARED]
 *
 *   [PAGE VIEW]     paginate(): the cards, in their order, on letter or landscape pages with the edges
 *                   drawn. A card keeps its width (12 columns across the page's printable width) and
 *                   height; a row that does not fit goes to the next page, never split; a card marked
 *                   Start a new page starts one. What is on screen is what prints.
 *   [PRINT TO PDF]  printPages(): the browser's own print dialog (Save as PDF) with the page size set and
 *                   nothing but the pages showing. Dark themes print on light paper.
 *   [MOVE]          makeMovable(): drag a card by its title bar and drop it before or after another
 *                   (PATCH /cards/{id} {"before": id}); the card menu's Move earlier / later does the same.
 */
import { api } from "./api";
import { h } from "./dom";

export type Paper = "letter" | "landscape";

export const PAPERS: Record<Paper, { label: string; width: number; height: number; css: string }> = {
  letter: { label: "Letter", width: 816, height: 1056, css: "8.5in 11in" },
  landscape: { label: "Letter landscape", width: 1056, height: 816, css: "11in 8.5in" },
};

const MARGIN = 48; // half an inch at 96 pixels to the inch
const GAP = 12;
const ROW_HEIGHT = 40;
const COLUMNS = 12;

export interface Placed {
  element: HTMLElement;
  size: { w: number; h: number };
  newPage: boolean;
}

const pixels = (rows: number): number => rows * ROW_HEIGHT + (rows - 1) * GAP;

/** A card's width in pixels on the page: w of 12 columns across the printable width. */
function widthOn(paper: Paper, w: number): number {
  const inner = PAPERS[paper].width - 2 * MARGIN;
  return ((inner - (COLUMNS - 1) * GAP) * w) / COLUMNS + (w - 1) * GAP;
}

/** A note's height at its width on the page: on paper nothing scrolls, so a note grows to show all of
 * its text (never shrinks below the height it was given). Measured inside `host` for its fonts. */
function measureNotes(items: Placed[], paper: Paper, host: HTMLElement): Map<Placed, number> {
  const heights = new Map<Placed, number>();
  const measurer = h("div", { class: "page-measure", "aria-hidden": "true" });
  host.append(measurer);
  for (const item of items) {
    if (!item.element.classList.contains("card-note")) continue;
    const before = item.element.style.cssText;
    item.element.style.width = `${widthOn(paper, item.size.w)}px`;
    item.element.style.height = "auto";
    item.element.style.flex = "none";
    measurer.append(item.element);
    heights.set(item, Math.ceil(item.element.scrollHeight) + 2);
    item.element.style.cssText = before;
  }
  measurer.remove();
  return heights;
}

/** The cards laid onto pages, in order. Each page is the paper's size with half-inch margins. */
export function paginate(items: Placed[], paper: Paper, host: HTMLElement): HTMLElement {
  const spec = PAPERS[paper];
  const usable = spec.height - 2 * MARGIN;
  const notes = measureNotes(items, paper, host);
  const tall = (item: Placed): number => Math.min(usable, Math.max(pixels(item.size.h), notes.get(item) ?? 0));
  const pages = h("div", { class: "pages", dataset: { paper } });
  pages.style.setProperty("--page-w", `${spec.width}px`);
  pages.style.setProperty("--page-h", `${spec.height}px`);
  let body: HTMLElement = h("div");
  let used = 0;
  let row: Placed[] = [];
  let rowWidth = 0;
  const startPage = (): void => {
    body = h("div", { class: "page-body" });
    pages.append(h("section", { class: "page", "aria-label": "Page" }, body));
    used = 0;
  };
  const flushRow = (): void => {
    if (!row.length) return;
    const height = Math.max(...row.map(tall));
    if (used > 0 && used + GAP + height > usable) startPage();
    for (const item of row) item.element.style.height = `${tall(item)}px`;
    body.append(h("div", { class: "page-row" }, ...row.map((item) => item.element)));
    used += (used ? GAP : 0) + height;
    row = [];
    rowWidth = 0;
  };
  startPage();
  for (const item of items) {
    if (item.newPage) {
      flushRow();
      if (used > 0) startPage();
    }
    if (row.length && rowWidth + item.size.w > COLUMNS) flushRow();
    row.push(item);
    rowWidth += item.size.w;
  }
  flushRow();
  const all = [...pages.querySelectorAll<HTMLElement>(".page")];
  all.forEach((page, i) => {
    page.setAttribute("aria-label", `Page ${i + 1} of ${all.length}`);
    page.append(h("footer", { class: "page-number" }, `${i + 1} / ${all.length}`));
  });
  return pages;
}

/** Shrink the pages to the panel's width on screen (never larger than life). */
export function fitPages(pages: HTMLElement, width: number): void {
  const paper = PAPERS[(pages.dataset.paper as Paper | undefined) ?? "letter"];
  pages.style.setProperty("zoom", String(Math.min(1, Math.max(0.3, (width - 32) / paper.width))));
}

/** Print the pages: the page size set, everything else hidden, then restored. `ready` runs first (it
 * switches a dark theme to light and waits for the charts to redraw); `done` runs after printing. */
export async function printPages(paper: Paper, ready: () => Promise<void>, done: () => void): Promise<void> {
  const style = h("style", { id: "sandbox-print-size" }, `@page { size: ${PAPERS[paper].css}; margin: 0; }`);
  document.head.append(style);
  document.body.classList.add("sandbox-printing");
  try {
    await ready();
    await new Promise<void>((resolve) => {
      const after = (): void => {
        window.removeEventListener("afterprint", after);
        resolve();
      };
      window.addEventListener("afterprint", after);
      window.print();
      // Some browsers print without firing afterprint; never leave the page in print mode.
      setTimeout(after, 1500);
    });
  } finally {
    style.remove();
    document.body.classList.remove("sandbox-printing");
    done();
  }
}

const DRAG_TYPE = "application/x-swissarmygrid-card";

/** Drag `handle` to move `element`'s card; drop on another card's left or right half to put it
 * before or after that card. `next` is the id of the card after this one (null when it is last). */
export function makeMovable(element: HTMLElement, handle: HTMLElement, id: string, next: string | null, moved: () => void): void {
  element.dataset.card = id;
  element.dataset.next = next ?? "";
  handle.draggable = true;
  handle.classList.add("card-handle");
  handle.title = "Drag to move";
  handle.addEventListener("dragstart", (event) => {
    event.dataTransfer?.setData(DRAG_TYPE, id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
    element.classList.add("dragging");
  });
  handle.addEventListener("dragend", () => {
    element.classList.remove("dragging");
    document.querySelectorAll(".drop-before, .drop-after").forEach((e) => e.classList.remove("drop-before", "drop-after"));
  });
  element.addEventListener("dragover", (event) => {
    if (!event.dataTransfer?.types.includes(DRAG_TYPE)) return;
    event.preventDefault();
    const box = element.getBoundingClientRect();
    const after = event.clientX - box.left > box.width / 2;
    element.classList.toggle("drop-after", after);
    element.classList.toggle("drop-before", !after);
  });
  element.addEventListener("dragleave", () => element.classList.remove("drop-before", "drop-after"));
  element.addEventListener("drop", (event) => {
    const dragged = event.dataTransfer?.getData(DRAG_TYPE);
    const after = element.classList.contains("drop-after");
    element.classList.remove("drop-before", "drop-after");
    if (!dragged || dragged === id) return;
    event.preventDefault();
    const before = after ? element.dataset.next || null : id;
    if (before === dragged) return;
    void moveCard(dragged, before).then(moved);
  });
}

/** Put a card before another (or last, with null). */
export async function moveCard(id: string, before: string | null): Promise<void> {
  await api(`/cards/${id}`, "PATCH", { before });
}
