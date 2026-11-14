/**
 * workbenchVariables.ts: the Workbench's Variables overlay, beside the document: what its kernel holds
 * now. [WIRE]
 *
 *   CONNECTIONS AND BLADES   each by its name in code: open (and what it holds) or not yet
 *   YOUR VARIABLES           name, type, size (rows by columns, a length), a short look at the value;
 *                            a table has Open as grid
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

interface Snapshot {
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
  toggle: (open?: boolean) => boolean;
}

export function variablesPanel(): VariablesPanel {
  const body = h("div", { class: "wb-vars-body" });
  const filter = h("input", { type: "search", class: "cat-search", placeholder: "Filter by name or type", "aria-label": "Filter variables" });
  const element = h(
    "aside",
    { class: "wb-vars", "aria-label": "Variables", hidden: true },
    h(
      "div",
      { class: "wb-vars-head" },
      h("h2", {}, "Variables"),
      button("Refresh", () => void refresh(), { icon: "back", iconOnly: true, class: "ghost", title: "Read the variables again" }),
      button("Close", () => toggle(false), { icon: "close", iconOnly: true, class: "ghost" }),
    ),
    filter,
    body,
  );
  let kernel: Kernel | null = null;
  let last: Snapshot | null = null;
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
      clear(body, h("p", { class: "hint" }, kernel ? "Nothing yet: the kernel starts on the first Run." : "Open a notebook or script."));
      return;
    }
    if (!last) {
      clear(body, h("p", { class: "hint" }, "Reading…"));
      return;
    }
    if (last.busy) {
      clear(body, h("p", { class: "hint" }, "Code is running: the variables show when it is done."));
      return;
    }
    const wanted = filter.value.trim().toLowerCase();
    const keep = (...words: string[]): boolean => !wanted || words.some((w) => w.toLowerCase().includes(wanted));
    const table = (head: string[], rows: HTMLElement[]): HTMLElement => h("table", { class: "wb-vars-table small" }, h("thead", {}, h("tr", {}, ...head.map((c) => h("th", { scope: "col" }, c)))), h("tbody", {}, ...rows));
    const connections = last.connections.filter((c) => keep(c.name, c.connection, c.type));
    const variables = last.variables.filter((v) => keep(v.name, v.type));
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
        : h("p", { class: "hint" }, "No connections yet (Connections, in the grid page's Application row)."),
      h("h3", {}, "Your variables"),
      variables.length
        ? table(
            ["Name", "Type", "Size", "Value"],
            variables.map((v) =>
              h(
                "tr",
                {},
                h("td", {}, h("code", {}, v.name)),
                h("td", { class: "wb-vars-type" }, v.type),
                h("td", { class: "num" }, v.size),
                h("td", { title: v.value }, h("span", { class: "wb-vars-value" }, v.value), v.table ? button(`Open ${v.name} as a grid`, () => void openAsGrid(v.name), { icon: "export", iconOnly: true, class: "ghost", title: "Open as grid" }) : null),
              ),
            ),
          )
        : h("p", { class: "hint" }, wanted ? "None matches." : "None yet."),
      h("h3", {}, "Environment"),
      environment.length
        ? table(["Variable", "In this kernel"], environment.map((e) => h("tr", {}, h("td", {}, h("code", {}, e.name)), h("td", {}, e.set ? "set (value hidden)" : h("span", { class: "hint" }, "not set")))))
        : h("p", { class: "hint" }, "No vault, secret blade or password names one."),
    );
  };

  const refresh = async (): Promise<void> => {
    if (element.hidden || !kernel?.id) return draw();
    try {
      last = await api<Snapshot>(`/workbench/kernels/${kernel.id}/variables`);
    } catch (e) {
      clear(body, h("p", { class: "status-error" }, (e as Error).message));
      return;
    }
    draw();
  };

  const toggle = (open?: boolean): boolean => {
    element.hidden = open === undefined ? !element.hidden : !open;
    if (!element.hidden) void refresh();
    return !element.hidden;
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
    toggle,
  };
}
