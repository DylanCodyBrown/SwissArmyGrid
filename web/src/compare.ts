/**
 * compare.ts: the two grids in a split, aligned row for row on key columns. [STATE + FUNCTION]
 *
 * The rule: the grids always align. Both panes read the same server mapping (routes_compare.py),
 * so row N on the left and row N on the right are the same key, or a shaded blank row on the side
 * that lacks it, and the two grids scroll together. Like a diff tool's two panes, for tables.
 *
 * Modes: outer join (every key from both sides), anti join (only unmatched rows: both sides, left
 * only or right only), cross compare (each grid shows its own rows; a row picked on one side
 * filters the other to that key). Keys come from the key picker (compareDialog.ts) when a split
 * is made, or from the ribbon's Compare row later.
 *
 * ROUTING: [START]          compareDialog Compare button -> startCompare(keys)
 *          [MODE / SIDE]    ribbon Compare row -> changeCompare({mode, anti_side})
 *          [STOP]           ribbon, or the split ending -> stopCompare()
 *          [ROW PICK]       aligned: a click lights up every row sharing its key, both sides -> showKeyGroup(row)
 *                           cross: a click filters the other grid to that key -> pick(side, row)
 *          [FOCUS BANNER]   per pane -> clearFocus()
 *          [CENTER SCROLLBAR] while aligned, one scrollbar between the grids drives both -> showSharedScroll()
 */
import { api } from "./api";
import { button, clear, formatCount, h, toast } from "./dom";
import { contextOf, onSplitChange, splitPair, type Context } from "./panes";

export interface CompareInfo {
  compare: string;
  left: string;
  right: string;
  keys: [string, string][];
  mode: "outer" | "anti" | "cross";
  anti_side: "both" | "left" | "right";
  row_count: number;
  counts: { matched: number; left_only: number; right_only: number };
  focus: { side: "left" | "right"; rows: number; key: unknown[] } | null;
}

let current: CompareInfo | null = null;
const listeners: (() => void)[] = [];
const linked = new WeakSet<object>();

export function currentCompare(): CompareInfo | null {
  return current;
}

/** The ribbon redraws its Compare row on every change. */
export function onCompareChange(listener: () => void): void {
  listeners.push(listener);
}

function changed(): void {
  for (const listener of listeners) listener();
}

// The compare belongs to the two grids it was made for: a different split (or none) ends it.
onSplitChange(() => {
  const pair = splitPair();
  if (current && (!pair || pair[0].id !== current.left || pair[1].id !== current.right)) void stopCompare();
  else changed();
});

export async function startCompare(keys: [string, string][], mode: CompareInfo["mode"] = "outer"): Promise<void> {
  const pair = splitPair();
  if (!pair) return;
  if (current) await stopCompare();
  try {
    current = await api<CompareInfo>("/compares", "POST", { left: pair[0].id, right: pair[1].id, keys, mode });
  } catch (e) {
    toast((e as Error).message, "error");
    return;
  }
  apply(pair);
  toast(`Aligned on ${keys.map(([l, r]) => (l === r ? l : `${l} = ${r}`)).join(" + ")}: ${formatCount(current.counts.matched)} matched, ${formatCount(current.counts.left_only)} left only, ${formatCount(current.counts.right_only)} right only.`);
}

export async function changeCompare(patch: Partial<Pick<CompareInfo, "keys" | "mode" | "anti_side">>): Promise<void> {
  const pair = splitPair();
  if (!current || !pair) return;
  try {
    current = await api<CompareInfo>(`/compares/${encodeURIComponent(current.compare)}`, "PATCH", patch);
  } catch (e) {
    toast((e as Error).message, "error");
    return;
  }
  apply(pair);
}

let recounting = 0;

/** The compare's counts again, after a side's view changed (debounced: both sides often change at once). */
async function recount(grid: string): Promise<void> {
  if (!current || (current.left !== grid && current.right !== grid)) return;
  window.clearTimeout(recounting);
  recounting = window.setTimeout(() => {
    const was = current;
    if (!was) return;
    void api<CompareInfo>(`/compares/${encodeURIComponent(was.compare)}`)
      .then((now) => {
        if (current?.compare !== was.compare) return;
        current = { ...now, focus: current.focus };
        changed();
      })
      .catch(() => undefined);
  }, 150);
}

export async function stopCompare(): Promise<void> {
  const was = current;
  current = null;
  // The compared grids by id: when the split itself ended (a side closed, "Close this side"),
  // splitPair() is already empty, and the grid left on screen kept showing the compare's rows.
  const compared = was ? [contextOf(was.left), contextOf(was.right)] : (splitPair() ?? []);
  for (const ctx of compared) {
    if (!ctx) continue;
    ctx.view?.setCompare(null);
    banner(ctx, null);
  }
  hideSharedScroll();
  if (was) await api(`/compares/${encodeURIComponent(was.compare)}`, "DELETE").catch(() => undefined);
  changed();
}

/** Point both panes at the compare, keep their scrolling level, and draw any focus banner. */
function apply([left, right]: [Context, Context]): void {
  const cmp = current!;
  left.view?.setCompare({ id: cmp.compare, side: "left" });
  right.view?.setCompare({ id: cmp.compare, side: "right" });
  // Each view is linked once; the handlers look up the current split when they fire.
  for (const mine of [left, right]) {
    if (!linked.has(mine.state)) {
      linked.add(mine.state);
      // A step on either side changes what lines up: the ribbon's counts are read again. A step made
      // here raises "view"; one from the server (the agent, a tie-out click) reloads, raising "info".
      mine.state.on("view", () => void recount(mine.id));
      mine.state.on("info", () => void recount(mine.id));
    }
    const view = mine.view;
    if (!view || linked.has(view)) continue;
    linked.add(view);
    view.onRowPick((row) => {
      const pairNow = splitPair();
      const at = pairNow?.findIndex((c) => c.view === view) ?? -1;
      if (!current || at < 0) return;
      if (current.mode === "cross") void pick(at === 0 ? "left" : "right", row);
      else void showKeyGroup(row);
    });
  }
  banner(left, cmp.focus?.side === "left" ? cmp.focus : null);
  banner(right, cmp.focus?.side === "right" ? cmp.focus : null);
  // Aligned grids share one scrollbar in the center; in cross compare each scrolls on its own.
  if (cmp.mode === "cross") hideSharedScroll();
  else showSharedScroll(left, right);
  changed();
}

// ===== the shared scrollbar between two aligned grids =====

let shared: { bar: HTMLElement; spacer: HTMLElement; timer: number; left: Context; right: Context; unlock: () => void } | null = null;

const viewportOf = (ctx: Context): HTMLElement | null => ctx.view?.host.querySelector<HTMLElement>(".ag-grid-viewport") ?? null;

/** One vertical scrollbar between the panes, the height of the grids' rows, driving both grids.
 * The grids' own vertical scrollbars hide meanwhile (wheel scrolling still works in either). */
function showSharedScroll(left: Context, right: Context): void {
  const panes = left.pane?.parentElement;
  if (!panes || !right.pane) return;
  if (!shared) {
    const spacer = h("div", { class: "center-scroll-spacer" });
    const bar = h("div", { class: "center-scroll", role: "scrollbar", "aria-label": "Scroll both grids", "aria-orientation": "vertical" }, spacer);
    shared = { bar, spacer, timer: 0, left, right, unlock: () => {} };
    // The row count (and so the scroll height) changes as windows load and steps change.
    shared.timer = window.setInterval(fitSharedScroll, 250);
  }
  shared.left = left;
  shared.right = right;
  panes.insertBefore(shared.bar, right.pane);
  panes.dataset.aligned = "on";
  fitSharedScroll();
  // Lock the two grids and the bar: whichever scrolls, the others take its exact position.
  shared.unlock();
  const elements = [viewportOf(left), shared.bar, viewportOf(right)].filter((e): e is HTMLElement => e !== null);
  shared.unlock = lockScroll(elements);
}

/** Keep `elements` at one vertical position. A native scroll listener on each sets the others to
 * its scrollTop in the same frame (no timers, no AG Grid events in between), so the two grids
 * can't drift apart. The echo from setting the others reads the same value and stops there. */
function lockScroll(elements: HTMLElement[]): () => void {
  const offs = elements.map((element) => {
    const follow = (): void => {
      for (const other of elements) if (other !== element && other.scrollTop !== element.scrollTop) other.scrollTop = element.scrollTop;
    };
    element.addEventListener("scroll", follow, { passive: true });
    return () => element.removeEventListener("scroll", follow);
  });
  return () => offs.forEach((off) => off());
}

/** Line the bar up with the grids' row area, and give it the same scroll height. */
function fitSharedScroll(): void {
  if (!shared) return;
  const viewport = viewportOf(shared.left);
  const panes = shared.bar.parentElement;
  if (!viewport || !panes) return;
  const box = viewport.getBoundingClientRect();
  const outer = panes.getBoundingClientRect();
  shared.bar.style.marginTop = `${Math.max(0, box.top - outer.top)}px`;
  shared.bar.style.height = `${viewport.clientHeight}px`;
  shared.spacer.style.height = `${viewport.scrollHeight}px`;
}

function hideSharedScroll(): void {
  if (!shared) return;
  const panes = shared.bar.parentElement;
  if (panes) delete panes.dataset.aligned;
  window.clearInterval(shared.timer);
  shared.unlock();
  shared.bar.remove();
  shared = null;
}

/** Aligned compare: light up every compare row that shares row's key, on both grids. Duplicate
 * keys pair up next to each other, so that is one block. */
async function showKeyGroup(row: number): Promise<void> {
  const pair = splitPair();
  if (!current || !pair) return;
  try {
    const group = await api<{ first: number; last: number }>(`/compares/${encodeURIComponent(current.compare)}/key-group?row=${row}`);
    for (const ctx of pair) ctx.view?.setKeyRows(group);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** Cross compare: a row picked on `side` filters the other grid to that row's key. */
async function pick(side: "left" | "right", row: number): Promise<void> {
  const pair = splitPair();
  if (!current || !pair) return;
  try {
    current = await api<CompareInfo>(`/compares/${encodeURIComponent(current.compare)}/focus`, "POST", { side, position: row });
  } catch (e) {
    toast((e as Error).message, "error");
    return;
  }
  const filtered = current.focus?.side === "left" ? pair[0] : pair[1];
  filtered.view?.setCompare({ id: current.compare, side: current.focus!.side });
  apply(pair);
}

export async function clearFocus(): Promise<void> {
  const pair = splitPair();
  if (!current || !pair) return;
  current = await api<CompareInfo>(`/compares/${encodeURIComponent(current.compare)}/focus`, "DELETE");
  apply(pair);
}

function banner(ctx: Context, focus: CompareInfo["focus"]): void {
  const slot = ctx.pane?.querySelector<HTMLElement>(".compare-slot");
  if (!slot) return;
  if (!focus) {
    clear(slot);
    return;
  }
  const key = focus.key.map((v) => (v === null ? "(blank)" : String(v))).join(", ");
  clear(
    slot,
    h(
      "div",
      { class: "preview-banner compare-banner", role: "status" },
      h("strong", {}, "Cross compare"),
      h("span", {}, `${formatCount(focus.rows)} row${focus.rows === 1 ? "" : "s"} with key ${key}`),
      h("span", { class: "spacer" }),
      button("Clear", () => void clearFocus(), { class: "primary" }),
    ),
  );
}
