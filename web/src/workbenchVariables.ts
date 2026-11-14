/**
 * workbenchVariables.ts: the Workbench's Variables pane (top right, RStudio's Environment): what the
 * front document's kernel holds now. [WIRE]
 *
 *   CONNECTIONS AND BLADES   each by its name in code: open (and what it holds) or not yet
 *   YOUR VARIABLES           name, type, size (rows by columns, a length), a short look at the value;
 *                            a table has Open as grid; a column's heading sorts by it
 *   ENVIRONMENT              the variables vaults, secret blades and connection passwords use: set or
 *                            not set (their values never leave the kernel)
 *
 * Read again after each run ends, and on Refresh. While code runs the kernel cannot answer: it says so.
 *
 * ROUTING: GET /workbench/kernels/{id}/variables; Open as grid -> POST /workbench/kernels/{id}/grid
 */
import { api } from "./api";
import { button, clear, h, toast } from "./dom";
import type { Kernel } from "./workbenchKernel";

export interface Snapshot {
  busy: boolean;
  connections: { name: string; connection: string; open: boolean; type: string; value: string; table: boolean }[];
  variables: { name: string; type: string; size: string; value: string; table: boolean }[];
  environment: { name: string; set: boolean }[];
}

export interface VariablesPanel {
  element: HTMLElement;
  /** Show this document's kernel (null: no document that runs code). */
  show: (kernel: Kernel | null) => void;
  refresh: () => Promise<void>;
  /** What the pane last read (null: nothing yet). */
  snapshot: () => Snapshot | null;
}

type SortKey = "name" | "type" | "size";
/** A size's first number ("1,200 rows by 4 columns" sorts as 1200). */
const sizeOf = (size: string): number => Number(/[\d,]+/.exec(size)?.[0]?.replace(/,/g, "") ?? -1);

export function variablesPanel(onClose: () => void = () => undefined): VariablesPanel {
  const body = h("div", { class: "wb-vars-body" });
  const filter = h("input", { type: "search", class: "cat-search", placeholder: "Filter", "aria-label": "Filter variables" });
  const element = h(
    "section",
    { class: "wb-vars", "aria-label": "Variables" },
    h(
      "div",
      { class: "wb-vars-head" },
      h("h2", {}, "Variables"),
      button("Refresh", () => void refresh(), { icon: "back", iconOnly: true, class: "ghost" }),
      button("Hide Variables", onClose, { icon: "close", iconOnly: true, class: "ghost" }),
    ),
    filter,
    body,
  );
  let kernel: Kernel | null = null;
  let last: Snapshot | null = null;
  let sort: { key: SortKey; down: boolean } = { key: "name", down: false };
  const watched = new WeakSet<Kernel>();

  const openAsGrid = async (name: string): Promise<void> => {
    if (!kernel?.id) return;
    try {
      await api(`/workbench/kernels/${kernel.id}/grid`, "POST", { name });
      toast(`${name} is open as a grid.`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const draw = (): void => {
    if (!kernel || !kernel.id) {
      clear(body, h("p", { class: "hint" }, kernel ? "Not started" : "Nothing open"));
      return;
    }
    if (!last) {
      clear(body, h("p", { class: "hint" }, "Reading…"));
      return;
    }
    if (last.busy) {
      clear(body, h("p", { class: "hint" }, "Running…"));
      return;
    }
    const wanted = filter.value.trim().toLowerCase();
    const keep = (...words: string[]): boolean => !wanted || words.some((w) => w.toLowerCase().includes(wanted));
    const table = (head: string[], rows: HTMLElement[], sortable = false): HTMLElement => {
      const keys: Record<string, SortKey> = { Name: "name", Type: "type", Size: "size" };
      const heading = (c: string): HTMLElement => {
        const key = keys[c];
        if (!sortable || !key) return h("th", { scope: "col" }, c);
        const on = sort.key === key;
        return h(
          "th",
          { scope: "col", "aria-sort": on ? (sort.down ? "descending" : "ascending") : "none" },
          h("button", { type: "button", class: "link wb-sort", onclick: () => { sort = { key, down: on ? !sort.down : false }; draw(); } }, c, on ? (sort.down ? " ▾" : " ▴") : ""),
        );
      };
      return h("table", { class: "wb-vars-table small" }, h("thead", {}, h("tr", {}, ...head.map(heading))), h("tbody", {}, ...rows));
    };
    const connections = last.connections.filter((c) => keep(c.name, c.connection, c.type));
    const variables = last.variables
      .filter((v) => keep(v.name, v.type))
      .sort((a, b) => {
        const by = sort.key === "size" ? sizeOf(a.size) - sizeOf(b.size) : a[sort.key].localeCompare(b[sort.key], undefined, { sensitivity: "base" });
        return (sort.down ? -by : by) || a.name.localeCompare(b.name);
      });
    const environment = last.environment.filter((e) => keep(e.name));
    clear(
      body,
      h("h3", {}, "Connections and blades"),
      connections.length
        ? table(
            ["Name", "Connection", "Value"],
            connections.map((c) =>
              h(
                "tr",
                { class: c.open ? "" : "not-open" },
                h("td", {}, h("code", {}, c.name)),
                h("td", {}, c.connection),
                h("td", { title: c.value }, c.open ? `${c.type}: ${c.value}` : h("span", { class: "hint" }, c.value), c.table ? button("Open as grid", () => void openAsGrid(c.name), { icon: "export", iconOnly: true, class: "ghost" }) : null),
              ),
            ),
          )
        : h("p", { class: "hint" }, "No connections"),
      h("h3", {}, "Your variables"),
      variables.length
        ? table(
            ["Name", "Type", "Size", "Value"],
            variables.map((v) =>
              h(
                "tr",
                {},
                h("td", {}, h("code", {}, v.name)),
                h("td", { class: "wb-vars-type", title: v.type }, v.type.split(".").pop() ?? v.type),
                h("td", { class: "num", title: v.size }, v.size.replace(/^([\d,]+) rows? by ([\d,]+) columns?$/, "$1 × $2")),
                h("td", { title: v.value }, h("span", { class: "wb-vars-value" }, v.value), v.table ? button(`Open ${v.name} as a grid`, () => void openAsGrid(v.name), { icon: "export", iconOnly: true, class: "ghost", title: "Open as grid" }) : null),
              ),
            ),
            true,
          )
        : h("p", { class: "hint" }, wanted ? "No matches" : "No variables"),
      h("h3", {}, "Environment"),
      environment.length
        ? table(["Variable", "In this kernel"], environment.map((e) => h("tr", {}, h("td", {}, h("code", {}, e.name)), h("td", {}, e.set ? "set" : h("span", { class: "hint" }, "not set")))))
        : h("p", { class: "hint" }, "None"),
    );
  };

  const refresh = async (): Promise<void> => {
    if (!element.isConnected || !element.offsetParent || !kernel?.id) return draw();
    try {
      last = await api<Snapshot>(`/workbench/kernels/${kernel.id}/variables`);
    } catch (e) {
      clear(body, h("p", { class: "status-error" }, (e as Error).message));
      return;
    }
    draw();
  };

  filter.addEventListener("input", draw);
  return {
    element,
    show: (next) => {
      kernel = next;
      last = null;
      // Read again whenever a run of this kernel ends (busy -> ready).
      if (next && !watched.has(next)) {
        watched.add(next);
        let was = next.state;
        next.onState(() => {
          if (was === "busy" && next.state === "idle" && kernel === next) void refresh();
          if (next.state === "off" && kernel === next) {
            last = null;
            draw();
          }
          was = next.state;
        });
      }
      void refresh();
    },
    refresh,
    snapshot: () => last,
  };
}
