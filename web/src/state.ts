/**
 * state.ts: one grid's view-model, and which grid is active. [STATE + FUNCTION]
 *
 * The page can hold several grids (tabs, and two side by side). Each has its own PageState. Two
 * kinds of listener: a grid's own view subscribes with onOwn() and hears every change to that
 * grid, even while another is active (an agent edit, a push); the panels and the ribbon subscribe
 * with on() and hear only the active grid. activate() switches the active grid and tells every
 * panel to redraw for it. `state` is a live binding: read it when you need it, never keep it.
 *
 * One object holds what every panel reads: the grid description from the server, the preview
 * position, the request generation and the selection. Panels subscribe; mutations go to the
 * server first and the state updates from its answer, so the server stays the source of
 * truth (a pop-out steps window or an agent changing the steps lands here the same way).
 */
import { api, clientId, gridPath, type GridInfo, type Settings, type StepJson } from "./api";

// "selection" = the status bar's numbers changed (also scrolling); "grid-selection" = what
// is selected changed (the toolbar's Column and Cell tools follow it).
type Topic = "info" | "preview" | "selection" | "grid-selection" | "view" | "preview-marks";

/** Excel's status bar over the selection, from the server (server/selection.py). */
export interface SelectionSummary {
  kind: string;
  rows: number;
  cells: number;
  count: number;
  numeric: number;
  sum: number | null;
  mean: number | null;
  min: number | null;
  max: number | null;
}
type Listener = () => void;

const TOPICS: Topic[] = ["info", "preview", "selection", "grid-selection", "view", "preview-marks"];
const emptyListeners = (): Record<Topic, Listener[]> => ({ info: [], preview: [], selection: [], "grid-selection": [], view: [], "preview-marks": [] });
// The panels' listeners: shared by every grid, called only for the active one.
const panelListeners = emptyListeners();

export class PageState {
  /** This grid's API path, /grids/<id>. */
  readonly base: string;
  info: GridInfo | null = null;
  /** Number of steps applied while previewing (null = the full view). */
  previewUpto: number | null = null;
  /** Bumped on every view change; rows requests carry it, older answers are ignored. */
  gen = 0;
  selection: SelectionSummary | null = null;
  /** Set while a SQL result replaces the grid: the bottom bar reports it instead. */
  sqlResult: { rows: number; milliseconds: number; capped: boolean } | null = null;
  /** Cells marked as changed in the previewed window. */
  changedCells = 0;
  private own = emptyListeners();

  constructor(readonly gridId: string) {
    this.base = gridPath(gridId);
  }

  /** A panel's listener: called when the active grid changes `topic` (and on activate). */
  on(topic: Topic, listener: Listener): void {
    panelListeners[topic].push(listener);
  }

  /** This grid's own listener (its view), called whether or not the grid is active. */
  onOwn(topic: Topic, listener: Listener): void {
    this.own[topic].push(listener);
  }

  emit(topic: Topic): void {
    for (const listener of this.own[topic]) listener();
    if (this === state) for (const listener of panelListeners[topic]) listener();
  }

  async load(): Promise<GridInfo> {
    this.info = await api<GridInfo>(this.base);
    this.emit("info");
    return this.info;
  }

  /** Replace the step list and/or settings on the server, then refresh everything. */
  async saveSteps(steps: StepJson[], settings?: Settings): Promise<GridInfo> {
    this.gen += 1;
    const body: Record<string, unknown> = {
      steps: steps.map(({ description: _d, ...rest }) => rest),
      gen: this.gen,
      client: clientId,
    };
    if (settings) body.settings = settings;
    this.info = await api<GridInfo>(`${this.base}/steps`, "POST", body);
    if (this.previewUpto !== null && this.previewUpto > steps.length) this.previewUpto = null;
    this.emit("info");
    this.emit("view");
    return this.info;
  }

  async addStep(step: StepJson): Promise<GridInfo> {
    const steps = [...(this.info?.steps ?? [])];
    // A new step goes before the header sort, so clicking a header stays "the last word".
    const headerAt = steps.findIndex((s) => s.params.header);
    if (headerAt >= 0) steps.splice(headerAt, 0, step);
    else steps.push(step);
    return this.saveSteps(steps);
  }

  /** The columns of what the grid shows: the previewed stage, or the full view. */
  previewColumns(): GridInfo["columns"] {
    const info = this.info;
    if (!info) return [];
    if (this.previewUpto === null) return info.columns;
    const stages = info.stage_columns;
    return stages[Math.min(this.previewUpto, stages.length - 1)] ?? info.columns;
  }

  setPreview(upto: number | null): void {
    this.previewUpto = upto;
    this.gen += 1;
    this.emit("preview");
    this.emit("view");
  }
}

/** The active grid. A placeholder until the page activates one. */
export let state = new PageState("");

/** Make `next` the active grid and have every panel redraw for it. */
export function activate(next: PageState): void {
  state = next;
  for (const topic of TOPICS) for (const listener of panelListeners[topic]) listener();
}
