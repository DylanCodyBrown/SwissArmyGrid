/**
 * panes.ts: every open grid in one page: the tab bar, a pane per grid, and the split. [STATE + WIRE]
 *
 * Each open grid has a context: its PageState and, once its tab is first opened, a pane with its
 * own grid view (kept while hidden, so switching back is instant). One grid is active at a time,
 * and the panels (Table Transformations, the analysis panel, the ribbon, the status bar, the SQL
 * box) always show the active one: activateGrid() points the API at it (useGrid), hands the
 * panels its view (setActiveView) and its state (activate), and they redraw.
 *
 * The split shows two panes side by side. Make one by dropping a tab on another tab, or by
 * dragging a tab down onto the grid (its left or right half says which side it goes). Click into
 * a pane to make it active; "Close this side" leaves the other grid alone.
 *
 * The Agent tab comes first, always, and can't be closed: it shows the Analysis Sandbox full
 * width (with a table picker) in place of the grids. Any grid tab goes back to the grids.
 *
 * The "+" after the last tab opens a draft tab: a new table from a query over the open tables
 * (queryTab.ts draws it). The draft's first Run builds the table, and its grid takes the draft's
 * place as an ordinary tab.
 *
 * ROUTING: [AGENT TAB]      click -> showAgentView()
 *          [TAB]            click -> activateGrid(id) + showGridView(); arrows, Home/End move; Delete closes
 *          [TAB CLOSE ×]    -> closeGrid(id)  (closes the grid on the server; the dataset stays)
 *          [DRAG TAB]       onto a tab's edge -> moveTab();  onto a tab's middle or the grid -> splitWith()
 *          [DRAFT TAB]      click -> showQueryView(); × -> closeDraft(); label -> renameDraft()
 *          [NEW TAB +]      -> newQueryTab()  (opens the draft, or brings the open one forward)
 *          [OPEN BUTTON]    -> openMenu.showOpenDialog()  (the tables in your running Python sessions)
 *          [PANE]           pointer down -> activateGrid(id)
 *          [CLOSE THIS SIDE]-> unsplit(keep)
 *          [PREVIEW BANNER] per pane -> state.setPreview(null)
 *          [EMPTY STATE]    -> read-only, when no grid is open
 */
import { api, useGrid } from "./api";
import { button, clear, h, icon, recall, remember, toast } from "./dom";
import { createGridView, setActiveView, type GridView } from "./gridView";
import { openMenuButton } from "./openMenu";
import { PageState, activate } from "./state";

export interface Context {
  id: string;
  dataset: string;
  state: PageState;
  view: GridView | null;
  pane: HTMLElement | null;
}

const contexts = new Map<string, Context>();
// What the page shows: the grids, or an app-level tab (Agent, ERD, the "+" draft) in their place.
type AppView = "grid" | "agent" | "erd" | "query";
let view: AppView = "grid";
const agentViewListeners: (() => void)[] = [];
// The "+" tab's draft: the name of the table its query will build. `serial` tells queryTab.ts a
// new draft from the one it last drew (a closed draft's query is not carried into the next).
let draft: { serial: number; name: string } | null = null;
let draftSerial = 0;

/** The Agent tab: the Analysis Sandbox full width, in place of the grids. */
export function showAgentView(): void {
  setView("agent");
}

/** The ERD tab: the relationship diagram of the open tables, in place of the grids. */
export function showErdView(): void {
  setView("erd");
}

export function showGridView(): void {
  setView("grid");
}

export function viewingAgent(): boolean {
  return view === "agent";
}

export function viewingErd(): boolean {
  return view === "erd";
}

export function viewingQuery(): boolean {
  return view === "query";
}

/** The draft tab: its query tray over a blank grid, in place of the grids. */
export function showQueryView(): void {
  setView("query");
}

export function currentDraft(): { serial: number; name: string } | null {
  return draft;
}

/** The "+": open a draft named query_<n> (the first n no dataset uses), or show the open one. */
export async function newQueryTab(): Promise<void> {
  if (!draft) {
    const { datasets } = await api<{ datasets: { name: string }[] }>("/datasets");
    const taken = new Set(datasets.map((d) => d.name));
    let n = 1;
    while (taken.has(`query_${n}`)) n += 1;
    draftSerial += 1;
    draft = { serial: draftSerial, name: `query_${n}` };
  }
  showQueryView();
}

/** The draft's tab label follows the name typed in its tray. */
export function renameDraft(name: string): void {
  if (!draft) return;
  draft.name = name;
  render();
}

/** Discard the draft (nothing was built) and go back to the grids. */
export function closeDraft(): void {
  draft = null;
  if (view === "query") showGridView();
  else render();
}

/** The draft's first Run built `dataset`: its grid takes the draft's place as an ordinary tab. */
export async function finishDraft(id: string, dataset: string): Promise<void> {
  draft = null;
  addGrid(id, dataset); // usually already added by the server's grid_shown notice
  showGridView();
  await activateGrid(id);
}

/** Called whenever the view changes (grids, Agent or ERD). */
export function onAgentView(listener: () => void): void {
  agentViewListeners.push(listener);
}

function setView(next: AppView): void {
  const changed = view !== next;
  view = next;
  const workspace = panesHost?.closest<HTMLElement>(".workspace");
  if (workspace) workspace.dataset.view = next;
  // The ribbon and the title act on a grid, so the app-level tabs hide them.
  document.body.classList.toggle("agent-view", next !== "grid");
  render();
  if (changed) for (const listener of agentViewListeners) listener();
}

/** Every open grid, in tab order, with its tab label (for the Agent tab's table picker). */
export function gridList(): { id: string; label: string }[] {
  return order.map((id) => ({ id, label: label(id) }));
}
let order: string[] = [];
let activeId: string | null = null;
// Counts activateGrid calls, so a slow first load can't take the front from a later click.
let activations = 0;
let split: [string, string] | null = null; // [left, right]
let dragging: string | null = null;
let tabRow: HTMLElement;
let panesHost: HTMLElement;
let onActivate: (state: PageState) => void = () => {};
// compare.ts listens: a new split opens the key picker; any change to the split re-checks the compare.
const splitCreated: ((left: Context, right: Context) => void)[] = [];
const splitChanged: (() => void)[] = [];
// queryTab.ts listens: a built table's pane gets its query tray, freed when the pane goes.
const paneCreated: ((ctx: Context) => void)[] = [];
const paneDropped: ((ctx: Context) => void)[] = [];

export function onPaneCreated(listener: (ctx: Context) => void): void {
  paneCreated.push(listener);
}

export function onPaneDropped(listener: (ctx: Context) => void): void {
  paneDropped.push(listener);
}

export function onSplitCreated(listener: (left: Context, right: Context) => void): void {
  splitCreated.push(listener);
}

export function onSplitChange(listener: () => void): void {
  splitChanged.push(listener);
}

/** The two grids side by side, left then right, or null when not split. */
export function splitPair(): [Context, Context] | null {
  const [left, right] = (split ?? []).map((id) => contexts.get(id));
  return left && right ? [left, right] : null;
}

export function mountPanes(tabs: HTMLElement, panes: HTMLElement, activated: (state: PageState) => void): void {
  tabRow = tabs;
  panesHost = panes;
  onActivate = activated;
  // Dragging a tab down onto the grid splits: the drop's half picks the side.
  panes.addEventListener("dragover", (e) => {
    if (!dragging || dragging === activeId || split) return;
    e.preventDefault();
    const right = e.clientX > panes.getBoundingClientRect().left + panes.clientWidth / 2;
    panes.dataset.drop = right ? "right" : "left";
  });
  panes.addEventListener("dragleave", (e) => { if (e.target === panes) delete panes.dataset.drop; });
  panes.addEventListener("drop", (e) => {
    const side = panes.dataset.drop as "left" | "right" | undefined;
    delete panes.dataset.drop;
    if (!dragging || !side || !activeId) return;
    e.preventDefault();
    void splitWith(dragging, activeId, side);
  });
}

export function contextOf(id: string): Context | undefined {
  return contexts.get(id);
}

export function contextsOf(dataset: string): Context[] {
  return [...contexts.values()].filter((c) => c.dataset === dataset);
}

export function activeContext(): Context | undefined {
  return activeId ? contexts.get(activeId) : undefined;
}

/** Load the open grids as tabs (remembered order first) and open `focus`, or the last active. */
export async function openGrids(focus: string | null): Promise<void> {
  const { grids } = await api<{ grids: { grid: string; dataset: string }[] }>("/grids");
  let saved: string[] = [];
  try {
    saved = JSON.parse(recall("workspace.order") ?? "[]") as string[];
  } catch {
    saved = [];
  }
  const open = new Map(grids.map((g) => [g.grid, g.dataset]));
  for (const id of saved) if (open.has(id)) addGrid(id, open.get(id)!);
  for (const g of grids) addGrid(g.grid, g.dataset);
  const remembered = recall("workspace.active");
  const first = (focus && contexts.has(focus) && focus) || (remembered && contexts.has(remembered) && remembered) || order[0];
  if (first) await activateGrid(first);
  else render();
}

/** After the live connection came back: notices sent while it was down never arrived, so match
 * the tabs to the server's grids and reload the ones already drawn. */
export async function resyncGrids(): Promise<void> {
  const { grids } = await api<{ grids: { grid: string; dataset: string }[] }>("/grids");
  const open = new Set(grids.map((g) => g.grid));
  for (const id of [...contexts.keys()]) if (!open.has(id)) dropGrid(id);
  for (const g of grids) addGrid(g.grid, g.dataset);
  for (const ctx of contexts.values()) {
    if (ctx.state.info) void ctx.state.load().then(() => ctx.view?.refresh()).catch(() => undefined);
  }
  if (!activeId && order[0]) await activateGrid(order[0]);
}

export function addGrid(id: string, dataset: string): void {
  if (contexts.has(id)) return;
  contexts.set(id, { id, dataset, state: new PageState(id), view: null, pane: null });
  order.push(id);
  render();
}

function label(id: string): string {
  const ctx = contexts.get(id)!;
  // Two grids on one dataset read "orders" and "orders · 2", in tab order.
  const same = order.filter((o) => contexts.get(o)?.dataset === ctx.dataset);
  return same.length > 1 ? `${ctx.dataset} · ${same.indexOf(id) + 1}` : ctx.dataset;
}

/** This grid's pane: its banner slot and its grid view, built the first time it is shown. */
function ensurePane(ctx: Context): HTMLElement {
  if (ctx.pane) return ctx.pane;
  const host = h("div", { class: "grid-host", "aria-label": `Data grid: ${ctx.dataset}` });
  const head = h(
    "header",
    { class: "pane-head" },
    h("strong", {}, ctx.dataset),
    h("span", { class: "spacer" }),
    button("Close this side", () => unsplit(split?.find((g) => g !== ctx.id) ?? ctx.id), { icon: "close", class: "ghost small", title: "Back to one grid (this one stays open as a tab)" }),
  );
  // queryTab.ts draws a built table's query tray here: its base data, above everything else.
  const querySlot = h("div", { class: "query-slot" });
  const slot = h("div", { class: "preview-slot" });
  // compare.ts draws the cross-compare banner ("Showing 2 rows with key 42 · Clear") here.
  const compareSlot = h("div", { class: "compare-slot" });
  ctx.pane = h("section", { class: "pane", "data-grid": ctx.id, hidden: true }, head, querySlot, slot, compareSlot, host);
  ctx.pane.addEventListener("pointerdown", () => { if (activeId !== ctx.id) void activateGrid(ctx.id); }, true);
  panesHost.append(ctx.pane);
  const view = createGridView(host, ctx.state);
  ctx.view = view;
  ctx.state.onOwn("view", () => view.refresh());
  ctx.state.onOwn("preview", () => { previewBanner(ctx, slot); view.refresh(); });
  ctx.state.onOwn("preview-marks", () => {
    const marks = slot.querySelector<HTMLElement>(".preview-marks");
    if (marks && ctx.state.changedCells) marks.textContent = `${marks.textContent?.split(" · ")[0]} · ${ctx.state.changedCells.toLocaleString()} cells changed by this step in the loaded rows are marked ✎.`;
  });
  for (const listener of paneCreated) listener(ctx);
  return ctx.pane;
}

function previewBanner(ctx: Context, slot: HTMLElement): void {
  const upto = ctx.state.previewUpto;
  if (upto === null) {
    clear(slot);
    return;
  }
  const step = upto === 0 ? "the source (before any step)" : `step ${upto}: ${ctx.state.info?.steps[upto - 1]?.description ?? ""}`;
  clear(
    slot,
    h(
      "div",
      { class: "preview-banner", id: "preview-banner", role: "status" },
      h("strong", {}, "Previewing"),
      h("span", { class: "preview-marks", id: "preview-marks" }, `the data after ${step}.`),
      h("span", { class: "spacer" }),
      button("Back to the full view", () => ctx.state.setPreview(null), { class: "primary" }),
    ),
  );
}

/** Make `id` the active grid: its pane shows (or takes the active side of the split), and every
 * panel redraws for it. */
export async function activateGrid(id: string): Promise<void> {
  const ctx = contexts.get(id);
  if (!ctx) return;
  const mine = ++activations;
  // Tell the server which table is in front, so an agent's prompt can say so (routes_context.py).
  void api("/workspace/active", "POST", { grid: id }).catch(() => undefined);
  if (!ctx.state.info) {
    try {
      await ctx.state.load();
    } catch (e) {
      if (mine === activations && contexts.get(id) === ctx) toast((e as Error).message, "error");
      return;
    }
    // Another tab was clicked (or this one closed) while this one loaded: the last click wins.
    if (mine !== activations || contexts.get(id) !== ctx) return;
  }
  if (split && !split.includes(id) && activeId) {
    // Opening another tab while split: it replaces the active side.
    split = split.map((g) => (g === activeId ? id : g)) as [string, string];
  }
  ensurePane(ctx);
  activeId = id;
  useGrid(id);
  setActiveView(ctx.view!);
  activate(ctx.state);
  onActivate(ctx.state);
  layout();
  remember("workspace.active", id);
  render();
}

function layout(): void {
  const shown = new Set(split ?? (activeId ? [activeId] : []));
  for (const ctx of contexts.values()) {
    if (!ctx.pane) continue;
    ctx.pane.hidden = !shown.has(ctx.id);
    ctx.pane.classList.toggle("active", ctx.id === activeId);
  }
  // Side by side, the page order is the screen order (screen readers read left to right too).
  const [left, right] = (split ?? []).map((id) => contexts.get(id)?.pane);
  if (left && right && left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_PRECEDING) {
    panesHost.insertBefore(left, right);
  }
  const was = panesHost.dataset.split === "on" ? panesHost.dataset.pair : "";
  panesHost.dataset.split = split ? "on" : "off";
  panesHost.dataset.pair = split ? split.join("|") : "";
  if (was !== panesHost.dataset.pair) for (const listener of splitChanged) listener();
}

/** Put `dragged` beside `beside`, on `side`. */
export async function splitWith(dragged: string, beside: string, side: "left" | "right"): Promise<void> {
  if (dragged === beside || !contexts.has(dragged) || !contexts.has(beside)) return;
  const besideCtx = contexts.get(beside)!;
  ensurePane(besideCtx);
  split = side === "left" ? [dragged, beside] : [beside, dragged];
  await activateGrid(dragged);
  const pair = splitPair();
  if (pair) for (const listener of splitCreated) listener(pair[0], pair[1]);
}

/** Back to one grid: `keep` stays on screen; the other remains open as a tab. */
export function unsplit(keep: string): void {
  split = null;
  void activateGrid(keep);
}

export async function closeGrid(id: string): Promise<void> {
  try {
    await api(`/grids/${encodeURIComponent(id)}`, "DELETE");
  } catch (e) {
    // Already closed on the server (a restart, an agent): just drop the tab.
    if ((e as { status?: number }).status !== 404) toast((e as Error).message, "error");
  }
  dropGrid(id);
}

/** Forget a grid that is gone, and show the one beside it. */
export function dropGrid(id: string): void {
  const ctx = contexts.get(id);
  if (!ctx) return;
  const at = order.indexOf(id);
  if (ctx.pane) for (const listener of paneDropped) listener(ctx);
  ctx.view?.destroy();
  ctx.pane?.remove();
  contexts.delete(id);
  order = order.filter((o) => o !== id);
  // Closing one side of a split leaves the other grid on screen.
  const other = split?.includes(id) ? split.find((g) => g !== id) : undefined;
  if (split?.includes(id)) split = null;
  remember("workspace.order", JSON.stringify(order));
  if (activeId === id || other) {
    activeId = null;
    const next = other ?? order[Math.min(at, order.length - 1)];
    if (next) {
      void activateGrid(next);
      return;
    }
  }
  layout();
  render();
}

function moveTab(from: string, before: string | null): void {
  if (from === before) return;
  order = order.filter((o) => o !== from);
  const at = before ? order.indexOf(before) : -1;
  order.splice(at < 0 ? order.length : at, 0, from);
  remember("workspace.order", JSON.stringify(order));
  render();
}

function render(): void {
  if (!tabRow) return;
  remember("workspace.order", JSON.stringify(order));
  clear(
    tabRow,
    // The Agent tab: always first, never closed or moved.
    h(
      "div",
      {
        class: `tab-item tab-app tab-agent ${view === "agent" ? "active" : ""}`,
        role: "tab",
        tabindex: view === "agent" ? "0" : "-1",
        "aria-selected": String(view === "agent"),
        title: "The Analysis Sandbox: charts, notes and result tables, for any table",
        onclick: () => showAgentView(),
        onkeydown: (e: KeyboardEvent) => {
          if (e.key === "ArrowRight" && order[0]) {
            showGridView();
            void activateGrid(order[0]);
          }
        },
      },
      icon("agent"),
      h("span", { class: "tab-label" }, "Agent"),
    ),
    // The ERD tab: always second, never closed or moved.
    h(
      "div",
      {
        class: `tab-item tab-app tab-erd ${view === "erd" ? "active" : ""}`,
        role: "tab",
        tabindex: view === "erd" ? "0" : "-1",
        "aria-selected": String(view === "erd"),
        title: "The ERD: the keys and joins between the open tables",
        onclick: () => showErdView(),
      },
      icon("crown"),
      h("span", { class: "tab-label" }, "ERD"),
    ),
    ...order.map((id) => {
      const selected = id === activeId && view === "grid";
      const inSplit = split?.includes(id) ?? false;
      const el = h(
        "div",
        {
          class: `tab-item ${selected ? "active" : ""} ${inSplit ? "in-split" : ""}`,
          "data-grid": id,
          role: "tab",
          tabindex: selected ? "0" : "-1",
          "aria-selected": String(selected),
          draggable: "true",
          title: `${contexts.get(id)!.dataset}. Drag onto another tab, or down onto the grid, to see both side by side; drag to a tab's edge to reorder.`,
          onclick: () => {
            showGridView();
            void activateGrid(id);
          },
          onauxclick: (e: MouseEvent) => { if (e.button === 1) void closeGrid(id); },
          onkeydown: (e: KeyboardEvent) => onTabKey(e, id),
          ondragstart: (e: DragEvent) => { dragging = id; e.dataTransfer?.setData("text/plain", id); },
          ondragend: () => { dragging = null; delete panesHost.dataset.drop; },
          ondragover: (e: DragEvent) => {
            e.preventDefault();
            el.classList.toggle("drop-before", edge(e, el));
            el.classList.toggle("drop-split", !edge(e, el));
          },
          ondragleave: () => el.classList.remove("drop-before", "drop-split"),
          ondrop: (e: DragEvent) => {
            e.preventDefault();
            el.classList.remove("drop-before", "drop-split");
            if (!dragging) return;
            if (edge(e, el)) moveTab(dragging, id);
            else void splitWith(dragging, id, "right");
          },
        },
        inSplit ? h("span", { class: "split-mark", "aria-label": "side by side" }, "◫") : null,
        h("span", { class: "tab-label" }, label(id)),
        closeButton(id, selected),
      );
      return el;
    }),
    draft ? draftTab(draft.name) : null,
    button("New table from a query", () => void newQueryTab(), { icon: "plus", iconOnly: true, class: "ghost tab-new", title: "New table: a query over the open tables, shown as they are in their tabs" }),
    openMenuButton(),
    h("div", {
      class: "tab-end",
      ondragover: (e: DragEvent) => e.preventDefault(),
      ondrop: (e: DragEvent) => { e.preventDefault(); if (dragging) moveTab(dragging, null); },
    }),
  );
  panesHost.querySelector(".workspace-empty")?.remove();
  if (!order.length) {
    panesHost.append(
      h(
        "div",
        { class: "workspace-empty", role: "status" },
        h("strong", {}, "No grids open."),
        h("span", {}, "Push data from Python and it opens here as a tab:"),
        h("code", {}, "import swissarmygrid as sag; sag.push(df)"),
      ),
    );
  }
}

/** The draft tab: after the grid tabs, named for the table its query will build. */
function draftTab(name: string): HTMLElement {
  const selected = view === "query";
  const close = button(`Close ${name || "the new query"}`, (e) => { e.stopPropagation(); closeDraft(); }, { icon: "close", iconOnly: true, class: "ghost tab-close" });
  if (!selected) close.tabIndex = -1;
  return h(
    "div",
    {
      class: `tab-item tab-draft ${selected ? "active" : ""}`,
      role: "tab",
      tabindex: selected ? "0" : "-1",
      "aria-selected": String(selected),
      title: "A new table from a query (not built yet: Run builds it)",
      onclick: () => showQueryView(),
      onauxclick: (e: MouseEvent) => { if (e.button === 1) closeDraft(); },
    },
    icon("sql"),
    h("span", { class: "tab-label" }, name || "New query"),
    close,
  );
}

/** A tab's ×. Only the active tab's takes a Tab stop: the bar is one stop (arrows move between
 * tabs, Delete closes one), so Tab doesn't walk through every tab's button. */
function closeButton(id: string, selected: boolean): HTMLButtonElement {
  const b = button(`Close ${label(id)}`, (e) => { e.stopPropagation(); void closeGrid(id); }, { icon: "close", iconOnly: true, class: "ghost tab-close" });
  if (!selected) b.tabIndex = -1;
  return b;
}

/** The left quarter of a tab is "put it before this tab"; the rest is "side by side". */
function edge(e: DragEvent, el: HTMLElement): boolean {
  const box = el.getBoundingClientRect();
  return e.clientX < box.left + box.width / 4;
}

function onTabKey(event: KeyboardEvent, id: string): void {
  const at = order.indexOf(id);
  const go = (to: number): void => {
    const next = order[(to + order.length) % order.length];
    if (next) void activateGrid(next).then(() => (tabRow.querySelector(".tab-item.active") as HTMLElement | null)?.focus());
  };
  if (event.key === "ArrowRight") go(at + 1);
  else if (event.key === "ArrowLeft") go(at - 1);
  else if (event.key === "Home") go(0);
  else if (event.key === "End") go(order.length - 1);
  else if (event.key === "Enter" || event.key === " ") void activateGrid(id);
  else if (event.key === "Delete") void closeGrid(id);
}
