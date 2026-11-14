/**
 * workbenchNotebook.ts: a notebook in the Workbench: cells of Python, SQL on a connection, or Markdown,
 * run in the document's IPython kernel. [WIRE]
 *
 *   [CELL]     its kind (Python, SQL, Markdown); a SQL cell's connection; Run; move up and down; delete
 *   [KEYS]     Shift+Enter runs and goes to the next cell (a new one after the last); Ctrl+Enter runs in
 *              place; a Markdown cell shows as text until double-clicked
 *   [OUTPUTS]  as the run streams them (workbenchKernel.ts), kept with the cell and saved with it
 *
 * A SQL cell runs as `%%sql <connection>` (client/sql_cell.py) in the kernel: its connection's own SQL,
 * or DuckDB over a file's tables.
 */
import { button, clear, h, toast } from "./dom";
import type { CodeEditor } from "./lazy/codeEditor";
import { renderMarkdown } from "./markdown";
import { pythonCompleter, sqlCompleter } from "./workbenchCompletion";
import { drawOutputs, type Kernel, type Output } from "./workbenchKernel";

/** A SQL cell's first line as the kernel runs it (domain/notebooks.py sql_line). */
export function sqlLine(connection: string | null): string {
  if (!connection) return "%%sql";
  return /\s/.test(connection) ? `%%sql '${connection.replace(/'/g, "'\"'\"'")}'` : `%%sql ${connection}`;
}

export type CellKind = "code" | "sql" | "markdown";
export interface CellData {
  kind: CellKind;
  source: string;
  connection: string | null;
  outputs: Output[];
  execution_count: number | null;
}

interface Cell extends CellData {
  element: HTMLElement;
  editor: CodeEditor | null;
  outputHost: HTMLElement;
  count: HTMLElement;
}

let editors: Promise<typeof import("./lazy/codeEditor")> | null = null;
const loadEditors = (): Promise<typeof import("./lazy/codeEditor")> =>
  (editors ??= import(/* external */ "/static/build/lazy/codeEditor.js" as string) as Promise<typeof import("./lazy/codeEditor")>);
const LANGUAGE: Record<CellKind, "python" | "sql" | "markdown"> = { code: "python", sql: "sql", markdown: "markdown" };
const KIND_NAMES: Record<CellKind, string> = { code: "Python", sql: "SQL", markdown: "Markdown" };

export interface NotebookView {
  element: HTMLElement;
  cells: () => CellData[];
  add: (kind: CellKind, source?: string) => void;
  runAll: () => Promise<void>;
  setTheme: (theme: string) => void;
}

export function notebookView(
  initial: CellData[],
  kernel: Kernel,
  connections: () => string[],
  theme: () => string,
  changed: () => void,
  blades: () => string[] = () => [],
): NotebookView {
  const python = pythonCompleter(kernel, () => ({ connections: connections(), blades: blades() }));
  const list = h("div", { class: "wb-cells" });
  const cells: Cell[] = [];
  let selected: Cell | null = null;
  const dark = (): boolean => theme().endsWith("dark");

  const select = (cell: Cell): void => {
    selected?.element.classList.remove("selected");
    selected = cell;
    cell.element.classList.add("selected");
  };

  const showMarkdown = (cell: Cell): void => {
    cell.outputHost.replaceChildren(renderMarkdown(cell.editor?.value() ?? cell.source));
    cell.element.classList.add("rendered");
  };

  const run = async (cell: Cell): Promise<boolean> => {
    if (cell.editor) cell.source = cell.editor.value();
    if (cell.kind === "markdown") {
      showMarkdown(cell);
      return true;
    }
    if (!cell.source.trim()) return true;
    cell.outputs = [];
    cell.count.textContent = "[*]";
    cell.element.classList.add("running");
    drawOutputs(cell.outputHost, [], dark());
    try {
      const done = await kernel.run({ code: cell.source, kind: cell.kind, connection: cell.connection }, (o) => {
        if ((o as { clear?: boolean }).clear) cell.outputs = [];
        else cell.outputs.push(o);
        drawOutputs(cell.outputHost, cell.outputs, dark());
      });
      cell.execution_count = done.execution_count;
      cell.count.textContent = done.execution_count ? `[${done.execution_count}]` : "[ ]";
      if (done.dead) toast("The kernel stopped (it may have run out of memory). Run again to start a new one.", "error");
      changed();
      return done.ok;
    } catch (e) {
      cell.count.textContent = "[!]";
      cell.outputs = [{ output_type: "error", ename: "", evalue: (e as Error).message, traceback: [(e as Error).message] }];
      drawOutputs(cell.outputHost, cell.outputs, dark());
      return false;
    } finally {
      cell.element.classList.remove("running");
    }
  };

  const indexOf = (cell: Cell): number => cells.indexOf(cell);

  const make = (data: CellData, at: number): Cell => {
    const editorHost = h("div", { class: "wb-editor" });
    const outputHost = h("div", { class: "wb-outputs", "aria-live": "polite" });
    const count = h("span", { class: "wb-count" }, data.execution_count ? `[${data.execution_count}]` : "[ ]");
    const kind = h("select", { "aria-label": "Cell kind" }, ...(["code", "sql", "markdown"] as CellKind[]).map((k) => h("option", { value: k, selected: k === data.kind }, KIND_NAMES[k])));
    const connection = h("select", { "aria-label": "Connection", class: "wb-connection", title: "The connection this SQL runs on" });
    const fillConnections = (): void => {
      const names = connections();
      const current = cell.connection;
      clear(
        connection,
        h("option", { value: "" }, "(the %sql_connect one)"),
        ...names.map((n) => h("option", { value: n, selected: n === current }, n)),
        ...(current && !names.includes(current) ? [h("option", { value: current, selected: true }, `${current} (not saved here)`)] : []),
      );
    };
    const element = h(
      "section",
      { class: `wb-cell kind-${data.kind}`, tabindex: -1 },
      h(
        "div",
        { class: "wb-cell-head" },
        count,
        kind,
        connection,
        h("span", { class: "spacer" }),
        button("Run cell", () => void run(cell), { icon: "play", iconOnly: true, class: "ghost", title: "Run (Ctrl+Enter; Shift+Enter runs and moves on)" }),
        button("Move up", () => move(cell, -1), { icon: "up", iconOnly: true, class: "ghost" }),
        button("Move down", () => move(cell, 1), { icon: "down", iconOnly: true, class: "ghost" }),
        button("Delete cell", () => remove(cell), { icon: "trash", iconOnly: true, class: "ghost" }),
      ),
      editorHost,
      outputHost,
    );
    const cell: Cell = { ...data, element, editor: null, outputHost, count };
    const sql = sqlCompleter(kernel, () => cell.connection);
    connection.hidden = data.kind !== "sql";
    fillConnections();
    connection.addEventListener("focus", fillConnections);
    connection.addEventListener("change", () => {
      cell.connection = connection.value || null;
      changed();
    });
    kind.addEventListener("change", () => {
      cell.kind = kind.value as CellKind;
      element.className = `wb-cell kind-${cell.kind}${selected === cell ? " selected" : ""}`;
      connection.hidden = cell.kind !== "sql";
      cell.editor?.setLanguage(LANGUAGE[cell.kind]);
      changed();
    });
    element.addEventListener("focusin", () => select(cell));
    element.addEventListener("dblclick", () => {
      if (cell.kind === "markdown" && element.classList.contains("rendered")) {
        element.classList.remove("rendered");
        cell.outputHost.replaceChildren();
        cell.editor?.focus();
      }
    });
    void loadEditors().then((m) => {
      cell.editor = m.createCodeEditor(editorHost, {
        language: LANGUAGE[cell.kind],
        value: cell.source,
        theme: theme(),
        grow: true,
        placeholder: cell.kind === "sql" ? "SELECT ... (on the connection chosen above)" : undefined,
        // By the cell's kind as it is now: Python from the kernel, SQL from the connection's catalog.
        complete: (text, offset) => (cell.kind === "sql" ? sql(text, offset) : cell.kind === "code" ? python(text, offset) : Promise.resolve([])),
        help: (text, offset) => (cell.kind === "code" ? kernel.help(text, offset) : Promise.resolve("")),
        keys: {
          shiftEnter: () => void run(cell).then(() => next(cell)),
          ctrlEnter: () => void run(cell),
        },
      });
      cell.editor.onChange(changed);
    });
    if (cell.kind === "markdown" && cell.source.trim()) showMarkdown(cell);
    else drawOutputs(outputHost, cell.outputs, dark());
    cells.splice(at, 0, cell);
    list.insertBefore(element, list.children[at] ?? null);
    return cell;
  };

  const next = (cell: Cell): void => {
    const following = cells[indexOf(cell) + 1] ?? make({ kind: cell.kind === "markdown" ? "code" : cell.kind, source: "", connection: cell.connection, outputs: [], execution_count: null }, cells.length);
    select(following);
    following.element.scrollIntoView({ block: "nearest" });
    setTimeout(() => following.editor?.focus(), 30);
  };

  const move = (cell: Cell, by: number): void => {
    const from = indexOf(cell);
    const to = from + by;
    if (to < 0 || to >= cells.length) return;
    cells.splice(from, 1);
    cells.splice(to, 0, cell);
    list.insertBefore(cell.element, by < 0 ? cells[to + 1]!.element : (cells[to + 1]?.element ?? null));
    changed();
  };

  const remove = (cell: Cell): void => {
    cells.splice(indexOf(cell), 1);
    cell.editor?.dispose();
    cell.element.remove();
    if (!cells.length) make({ kind: "code", source: "", connection: null, outputs: [], execution_count: null }, 0);
    changed();
  };

  for (const data of initial.length ? initial : [{ kind: "code" as CellKind, source: "", connection: null, outputs: [], execution_count: null }]) make(data, cells.length);

  return {
    element: list,
    cells: () => cells.map((c) => ({ kind: c.kind, source: c.editor ? c.editor.value() : c.source, connection: c.kind === "sql" ? c.connection : null, outputs: c.kind === "markdown" ? [] : c.outputs, execution_count: c.execution_count })),
    add: (kind, source = "") => {
      const at = selected ? indexOf(selected) + 1 : cells.length;
      const cell = make({ kind, source, connection: kind === "sql" ? (connections()[0] ?? null) : null, outputs: [], execution_count: null }, at);
      select(cell);
      cell.element.scrollIntoView({ block: "nearest" });
      setTimeout(() => cell.editor?.focus(), 50);
      changed();
    },
    runAll: async () => {
      for (const cell of [...cells]) {
        if (!(await run(cell))) break; // stop at the first error, as Jupyter's Run all does
      }
    },
    setTheme: (t) => {
      for (const c of cells) c.editor?.setTheme(t);
    },
  };
}
