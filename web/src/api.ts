/**
 * api.ts: the page's one door to the server. [SOURCE]
 *
 * The token arrives in the URL fragment (#token=...), which the browser never sends to the
 * server. Every request here attaches it: a Bearer header on fetch, a subprotocol on the
 * WebSocket (browsers cannot set WebSocket headers). Think of this as the typed HttpClient
 * wrapper a C# front end would inject everywhere.
 */

// ===== TYPES: the shapes the server sends (server/routes_*.py) =====

export type Row = unknown[];

export interface ColumnInfo {
  name: string;
  type: string; // DuckDB type name: BIGINT, DOUBLE, VARCHAR, DATE, ...
}

export interface StepJson {
  id?: string;
  kind: string;
  params: Record<string, unknown>;
  enabled?: boolean;
  description?: string;
}

export interface Settings {
  case_sensitive: boolean;
  wildcards: boolean;
  trim_spaces: boolean;
  empty_is_blank: boolean;
}

export interface Budget {
  used_bytes: number;
  total_bytes: number;
  used: string;
  total: string;
  datasets: number;
  max_datasets: number;
  grids: number;
  max_grids: number;
  warn: boolean;
}

export interface GridInfo {
  grid: string;
  dataset: string;
  title: string;
  version: number;
  row_count: number;
  source_kind: string;
  /** The SELECT that built this dataset (a "+" tab's table), or null for data pushed or loaded. */
  query: string | null;
  source_columns: ColumnInfo[];
  columns: ColumnInfo[];
  stage_columns: ColumnInfo[][];
  steps: StepJson[];
  step_error: { index: number; message: string } | null;
  settings: Settings;
  gen: number;
  budget: Budget;
}

export interface RowsResponse {
  rows: Row[];
  row_count: number;
  columns: string[];
  truncated: [number, number][];
  highlighted: [number, string][];
  version: number;
  gen: number | null;
}

export interface Notice {
  type: string;
  dataset?: string;
  grid?: string;
  version?: number;
  by?: string;
  id?: string;
  action?: string;
  detail?: string;
  sql?: string;
  rows?: number;
  label?: string;
  name?: string;
  message?: string;
  area?: string;
  scale?: number;
  process?: string;
}

// ===== the token and the grid id, from the address =====

/** The active grid's id and API path. Live bindings: useGrid() switches them when another grid
 * (a tab, or the other side of a split) becomes active, and every module reads the new value. */
export let gridId: string = location.pathname.startsWith("/g/") ? decodeURIComponent(location.pathname.slice(3)) : "";
export let grid = gridPath(gridId);

export function gridPath(id: string): string {
  return `/grids/${encodeURIComponent(id)}`;
}

export function useGrid(id: string): void {
  gridId = id;
  grid = gridPath(id);
}
/** The fragment as the page was opened with (#token=...&grid=...), kept because reading the
 * token clears it from the address bar. The workspace reads `grid` from here. */
export const openedWith: URLSearchParams = new URLSearchParams(location.hash.slice(1));
/** Read from the URL fragment once, then removed from the address bar (so it is not on screen
 * or in a screenshot) and kept in this tab's session storage so a reload still works. */
export const token: string = (() => {
  const key = `swissarmygrid.token.${location.pathname}`;
  const fromUrl = new URLSearchParams(location.hash.slice(1)).get("token");
  let found = fromUrl ?? "";
  try {
    if (fromUrl) sessionStorage.setItem(key, fromUrl);
    else found = sessionStorage.getItem(key) ?? "";
  } catch {
    // Storage blocked: the token lives only in this page; a reload needs the full link again.
  }
  if (fromUrl) history.replaceState(null, "", location.pathname + location.search);
  return found;
})();
/** This page's id: request generations are counted per page, so two tabs on one grid never
 * make each other's requests look stale. */
export const clientId: string = Math.random().toString(36).slice(2, 12);

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function headers(json: boolean): Record<string, string> {
  const out: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (json) out["Content-Type"] = "application/json";
  return out;
}

/** GET/POST/PATCH/DELETE returning JSON; a non-2xx status throws ApiError with the server's
 * plain-language message. */
export async function api<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: headers(body !== undefined),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw new ApiError(response.status, (await response.text()) || response.statusText);
  return (await response.json()) as T;
}

/** A download (export): returns the bytes, the file name and the server's summary message. */
export async function download(path: string): Promise<{ blob: Blob; name: string; message: string }> {
  const response = await fetch(path, { headers: headers(false) });
  if (!response.ok) throw new ApiError(response.status, await response.text());
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "export";
  return { blob: await response.blob(), name, message: response.headers.get("X-Export-Message") ?? "" };
}

export function query(params: Record<string, string | number | undefined | null>): string {
  const pairs = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return pairs.length ? "?" + new URLSearchParams(pairs.map(([k, v]) => [k, String(v)])).toString() : "";
}

// ===== live updates =====

type Listener = (notice: Notice) => void;
const listeners: Listener[] = [];

export function onNotice(listener: Listener): void {
  listeners.push(listener);
}

/** The live connection. `onState(live, again)` hears each open and close; `again` is true on an
 * open after the first, when notices may have been missed while the socket was down.
 *
 * A dropped socket is opened again while this server still knows the token (it answers /health
 * with it): a stall or a network blip no longer leaves the page "not live" for good, which also
 * made every later push open a new window instead of a tab here. A restarted server has a new
 * token, so this page's link is dead and it stops trying. */
export function connect(onState: (live: boolean, again: boolean) => void, who: Record<string, string> = { grid: gridId }): void {
  let delay = 1000;
  let opened = false;
  const retryLater = (): void => {
    window.setTimeout(() => void retry(), delay);
    delay = Math.min(delay * 2, 15000);
  };
  const retry = async (): Promise<void> => {
    try {
      const health = await fetch("/health", { headers: headers(false) });
      if (health.status === 401 || health.status === 403) return; // another server now: the link is dead
      if (!health.ok) throw new Error(health.statusText);
    } catch {
      retryLater(); // not answering yet
      return;
    }
    open();
  };
  const open = (): void => {
    // ?grid= tells the server which grid this page shows (?workspace=1: a tab bar that shows every
    // grid), so a push knows its grid is on screen and opens no new window.
    const socket = new WebSocket(`ws://${location.host}/ws?${new URLSearchParams(who).toString()}`, ["swissarmygrid", token]);
    let live = false;
    socket.addEventListener("open", () => {
      live = true;
      delay = 1000;
      onState(true, opened);
      opened = true;
    });
    socket.addEventListener("close", () => {
      if (live) onState(false, false);
      retryLater();
    });
    socket.addEventListener("message", (event: MessageEvent<string>) => {
      let notice: Notice;
      try {
        notice = JSON.parse(event.data) as Notice;
      } catch {
        return;
      }
      // One listener's failure must not keep the notice from the others.
      for (const listener of listeners) {
        try {
          listener(notice);
        } catch (error) {
          console.error(error);
        }
      }
    });
  };
  open();
}
