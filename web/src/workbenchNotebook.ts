/**
 * workbenchNotebook.ts: a notebook in the Workbench: cells of Python, SQL on a connection, or Markdown,
 * run in the document's IPython kernel. [WIRE]
 *
 *   [CELL]     its kind (Python, SQL, Markdown); a SQL cell's connection; Run; More: run above, run
 *              below, insert above or below, duplicate, clear or hide its output; move up and down; delete
 *   [EDIT]     Shift+Enter runs and goes to the next cell (a new one after the last); Ctrl+Enter runs in
 *              place; Escape leaves the editor for command mode; a Markdown cell shows as text until
 *              double-clicked (or Enter)
 *   [COMMAND]  Jupyter's keys on the selected cell: Enter edits; A and B insert above and below; D D
 *              deletes; Y, M and S make it Python, Markdown or SQL; J and K (or Down and Up) move the
 *              selection; Z undoes a delete or move; Shift+Enter and Ctrl+Enter run
 *   [OUTPUTS]  as the run streams them (workbenchOutputs.ts), kept with the cell and saved with it; a
 *              box when the code asks for a line (input()); a word when the kernel stopped or restarted
 *   [ERRORS]   the error panel in the cell's output (workbenchErrors.ts); the failing expression marked
 *              in its cell (and the calls that led there in theirs), with a hover card and Ctrl+. fixes;
 *              a frame or "In your code" goes to its cell and line; "Run the cell that makes x" finds it
 *
 * A SQL cell runs as `%%sql <connection>` (client/sql_cell.py) in the kernel: its connection's own SQL,
 * or DuckDB over a file's tables.
 */
import { button, clear, h, toast } from "./dom";
import type { CodeEditor } from "./lazy/codeEditor";
import { renderMarkdown } from "./markdown";
import { codeItem, currentDocument, sendToAgent } from "./workbenchAgent";
import { loadEditors } from "./workbenchConsole";
import { pythonCompleter, sqlCompleter } from "./workbenchCompletion";
import { type ErrorDetail, type Fix, type Frame, problemsFor, sqlProblems, yourFrame } from "./workbenchErrors";
import { type Kernel, runInto } from "./workbenchKernel";
import { type ErrorHooks, type Output, type OutputArea, outputArea, resultName } from "./workbenchOutputs";

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

interface Cell extends Omit<CellData, "outputs"> {
  element: HTMLElement;
  editor: CodeEditor | null;
  area: OutputArea;
  /** A Markdown cell drawn as text. */
  view: HTMLElement;
  count: HTMLElement;
  kindPicker: HTMLSelectElement;
  connectionPicker: HTMLSelectElement;
  /** Give it the keyboard once its editor exists, unless the focus went elsewhere meanwhile. */
  wantsFocus: boolean;
}

/** What Undo puts back. */
type Undo = { kind: "delete"; data: CellData; at: number; filler: Cell | null } | { kind: "move"; cell: Cell; from: number };

const LANGUAGE: Record<CellKind, "python" | "sql" | "markdown"> = { code: "python", sql: "sql", markdown: "markdown" };
const KIND_NAMES: Record<CellKind, string> = { code: "Python", sql: "SQL", markdown: "Markdown" };
const empty = (kind: CellKind = "code", connection: string | null = null): CellData => ({ kind, source: "", connection, outputs: [], execution_count: null });

export interface NotebookView {
  element: HTMLElement;
  cells: () => CellData[];
  /** Add a cell after the selected one (`first`: at the top). */
  add: (kind: CellKind, source?: string, first?: boolean) => void;
  /** Add a cell above or below the selected one, and select it. */
  insert: (where: "above" | "below", kind?: CellKind, source?: string) => void;
  runAll: () => Promise<void>;
  /** Run the selected cell (or the one at `index`, from 0). */
  runCell: (index?: number) => Promise<boolean>;
  runAbove: () => Promise<void>;
  runBelow: () => Promise<void>;
  selected: () => number;
  select: (index: number, edit?: boolean) => void;
  remove: (index?: number) => void;
  move: (by: -1 | 1) => void;
  setKind: (kind: CellKind) => void;
  duplicate: () => void;
  clearOutput: (index?: number) => void;
  clearAll: () => void;
  toggleOutput: () => void;
  /** Leave the editor for command mode (the keys act on the cell). */
  commandMode: () => void;
  /** Put back the last deleted or moved cell (false: nothing to undo). */
  undo: () => boolean;
  /** The help for the name at the cursor in the selected cell. */
  helpAtCursor: () => Promise<string>;
  setTheme: (theme: string) => void;
}

export function notebookView(
  initial: CellData[],
  kernel: Kernel,
  connections: () => string[],
  theme: () => string,
  changed: () => void,
  blades: () => string[] = () => [],
  codeNames: () => { name: string; connection: string; kind: string }[] = () => [],
  hooks: { cursor?: (line: number, column: number) => void; help?: () => void; fix?: (fix: Fix) => boolean; openGrid?: (name: string) => void; openResult?: (count: number, name?: string) => void } = {},
): NotebookView {
  const python = pythonCompleter(kernel, () => ({ connections: connections(), blades: blades(), codeNames: codeNames() }));
  const list = h("div", { class: "wb-cells", role: "list", "aria-label": "Cells" });
  const cells: Cell[] = [];
  let selected: Cell | null = null;
  const undos: Undo[] = [];
  let pendingD = 0; // the first D of D D, and when
  const dark = (): boolean => theme().endsWith("dark");
  const indexOf = (cell: Cell): number => cells.indexOf(cell);

  const label = (cell: Cell): string => `Cell ${indexOf(cell) + 1} of ${cells.length}, ${KIND_NAMES[cell.kind]}`;
  const relabel = (): void => {
    for (const c of cells) c.element.setAttribute("aria-label", label(c));
  };

  const select = (cell: Cell): void => {
    if (selected && selected !== cell) selected.element.classList.remove("selected");
    selected = cell;
    cell.element.classList.add("selected");
  };

  const showMarkdown = (cell: Cell): void => {
    cell.view.replaceChildren(renderMarkdown(cell.editor?.value() ?? cell.source));
    cell.element.classList.add("rendered");
  };

  const editMarkdown = (cell: Cell): void => {
    cell.element.classList.remove("rendered");
    cell.view.replaceChildren();
    setTimeout(() => cell.editor?.focus(), 0);
  };

  const run = async (cell: Cell): Promise<boolean> => {
    if (cell.editor) cell.source = cell.editor.value();
    for (const c of cells) c.editor?.setProblems([]); // the last failure's marks describe code that ran before
    if (cell.kind === "markdown") {
      showMarkdown(cell);
      return true;
    }
    if (!cell.source.trim()) return true;
    cell.area.set([]);
    cell.element.classList.remove("output-hidden");
    cell.count.textContent = "[*]";
    cell.element.classList.add("running");
    try {
      const done = await runInto(kernel, { code: cell.source, kind: cell.kind === "sql" ? "sql" : "code", connection: cell.connection }, cell.area);
      cell.execution_count = done.execution_count;
      cell.count.textContent = done.error ? "[!]" : done.execution_count ? `[${done.execution_count}]` : "[ ]";
      cell.element.classList.toggle("failed", !done.ok);
      changed();
      return done.ok;
    } finally {
      cell.element.classList.remove("running");
    }
  };

  /** Run cells in order, stopping at the first that fails (or a stopped kernel), as Jupyter does. */
  const runEach = async (some: Cell[]): Promise<void> => {
    for (const cell of some) {
      if (!(await run(cell))) break;
    }
  };

  const setKind = (cell: Cell, kind: CellKind): void => {
    if (cell.kind === kind) return;
    if (cell.kind === "markdown" && cell.element.classList.contains("rendered")) editMarkdown(cell);
    cell.kind = kind;
    cell.kindPicker.value = kind;
    cell.element.classList.remove("kind-code", "kind-sql", "kind-markdown");
    cell.element.classList.add(`kind-${kind}`);
    cell.connectionPicker.hidden = kind !== "sql";
    if (kind === "sql" && !cell.connection) cell.connection = connections()[0] ?? null;
    // The box shows the connection the cell runs on (it is drawn again on focus; once showed the
    // %sql_connect placeholder while the cell ran on the first connection).
    if (kind === "sql") cell.connectionPicker.dispatchEvent(new Event("focus"));
    cell.editor?.setLanguage(LANGUAGE[kind]);
    relabel();
    changed();
  };

  const cellMenu = (cell: Cell, anchor: HTMLElement): void => {
    document.querySelector(".menu")?.remove();
    const item = (text: string, act: () => void): HTMLElement =>
      h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); select(cell); act(); } }, text);
    const at = (): number => indexOf(cell);
    const menu = h(
      "div",
      { class: "menu", role: "menu", "aria-label": "Cell actions" },
      item("Run the cells above", () => void runEach(cells.slice(0, at()))),
      item("Run this cell and the ones below", () => void runEach(cells.slice(at()))),
      item("Insert a cell above", () => insertAt(at(), cell.kind === "markdown" ? "code" : cell.kind)),
      item("Insert a cell below", () => insertAt(at() + 1, cell.kind === "markdown" ? "code" : cell.kind)),
      item("Duplicate", () => duplicate(cell)),
      item("Clear the output", () => clearOutput(cell)),
      item(cell.element.classList.contains("output-hidden") ? "Show the output" : "Hide the output", () => toggleOutput(cell)),
    );
    document.body.append(menu);
    const box = anchor.getBoundingClientRect();
    menu.style.top = `${box.bottom + 4}px`;
    menu.style.left = `${Math.max(8, Math.min(box.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
    (menu.querySelector("button") as HTMLButtonElement | null)?.focus();
    setTimeout(() => document.addEventListener("mousedown", function away(e) { if (!menu.contains(e.target as Node)) { menu.remove(); document.removeEventListener("mousedown", away); } }));
    menu.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        menu.remove();
        anchor.focus();
      }
    });
  };

  // ===== errors: which cell a frame is in, its marks, its fixes =====

  /** The cell a frame ran in: the one with that run number, or `running` for the newest number. */
  const cellOf = (frame: Pick<Frame, "where" | "cell">, detail: ErrorDetail, running: Cell): Cell | null => {
    if (frame.where !== "cell" || frame.cell === null) return null;
    const numbers = detail.chain.flatMap((e) => e.frames.map((f) => f.cell ?? 0)).concat(detail.chain.map((e) => e.syntax?.cell ?? 0));
    if (frame.cell === Math.max(...numbers)) return running;
    return cells.find((c) => c !== running && c.execution_count === frame.cell) ?? null;
  };

  const errorHooks = (running: Cell): ErrorHooks => {
    let shown: ErrorDetail | null = null;
    const where = (): Cell => {
      const entry = shown?.chain[shown.chain.length - 1];
      const frame = yourFrame(entry) ?? (entry?.syntax ? ({ where: "cell", cell: entry.syntax.cell } as Frame) : null);
      return (frame && shown && cellOf(frame, shown, running)) || running;
    };
    return {
      shown: (detail) => {
        shown = detail;
        if (running.kind === "sql") {
          running.editor?.setProblems(sqlProblems(detail));
          return;
        }
        for (const c of cells) {
          const list = problemsFor(detail, (f) => (cellOf(f, detail, running) === c ? { line: f.line, colShift: 0 } : null));
          if (list.length) c.editor?.setProblems(list);
        }
      },
      fix: (fix) => {
        if (fix.kind === "replace") {
          const target = where();
          const entry = shown?.chain[shown.chain.length - 1];
          const near = running.kind === "sql" ? (entry?.sql?.line ?? 1) : (yourFrame(entry)?.line ?? entry?.syntax?.line ?? 1);
          if (!target.editor?.applyFix(fix, near)) toast(`${fix.target ?? "That"} is no longer in the cell.`, "error");
          else {
            select(target);
            target.editor.focus(); // back in the code, ready to run it again
          }
          return;
        }
        if (fix.kind === "import") {
          const first = cells.find((c) => c.kind === "code") ?? running;
          first.editor?.applyFix(fix, 1);
          select(first);
          first.editor?.focus();
          toast(`Added to cell ${indexOf(first) + 1}`);
          return;
        }
        if (fix.kind === "define" && fix.name) {
          const maker = definingCell(fix.name, running);
          if (maker) {
            void run(maker).then((ok) => ok && toast(`Ran cell ${indexOf(maker) + 1}`));
          } else toast(`No cell above defines ${fix.name}`, "error");
          return;
        }
        if (!hooks.fix?.(fix)) toast("Fix not available here", "error");
      },
      goTo: (frame) => {
        const target = shown ? cellOf(frame, shown, running) : null;
        if (!target) return false;
        select(target);
        target.element.scrollIntoView({ block: "nearest" });
        target.editor?.reveal(frame.line, frame.col ?? 1);
        return true;
      },
      openGrid: hooks.openGrid,
    };
  };

  const kept = new WeakMap<Cell, ErrorHooks>();
  const hooksOf = (cell: Cell): ErrorHooks => {
    let found = kept.get(cell);
    if (!found) kept.set(cell, (found = errorHooks(cell)));
    return found;
  };

  /** The cell above `before` that makes `name` (assigns, defines or imports it), nearest first. */
  const definingCell = (name: string, before: Cell): Cell | null => {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const makes = new RegExp(`^\\s*(?:${escaped}\\s*(?:,[^=\\n]*)?=(?!=)|(?:[\\w\\s,]*,\\s*)${escaped}\\s*=(?!=)|def ${escaped}\\b|class ${escaped}\\b|import\\s.*\\bas\\s+${escaped}\\b|from\\s.*\\bimport\\s.*\\b${escaped}\\b|import\\s+${escaped}\\b|for\\s.*\\b${escaped}\\b.*\\bin\\b)`, "m");
    const above = cells.slice(0, Math.max(0, indexOf(before)));
    for (const c of [...above].reverse()) {
      if (c.kind === "code" && makes.test(c.editor?.value() ?? c.source)) return c;
    }
    return null;
  };

  const make = (data: CellData, at: number): Cell => {
    const editorHost = h("div", { class: "wb-editor" });
    // The hooks need the cell, made just below: they look it up when an error comes.
    let self: Cell | null = null;
    const area = outputArea(dark, () => undefined, {
      shown: (d) => self && hooksOf(self).shown?.(d),
      fix: (f, d) => self && hooksOf(self).fix(f, d),
      goTo: (f) => (self ? hooksOf(self).goTo(f) : false),
      openGrid: hooks.openGrid,
      openResult: (n) => hooks.openResult?.(n, resultName(self?.editor?.value() ?? self?.source ?? "")),
    });
    const view = h("div", { class: "wb-markdown-view" });
    const count = h("span", { class: "wb-count" }, data.execution_count ? `[${data.execution_count}]` : "[ ]");
    const kindPicker = h("select", { "aria-label": "Cell kind" }, ...(["code", "sql", "markdown"] as CellKind[]).map((k) => h("option", { value: k, selected: k === data.kind }, KIND_NAMES[k]))) as HTMLSelectElement;
    const connectionPicker = h("select", { "aria-label": "Connection", class: "wb-connection", title: "Connection" }) as HTMLSelectElement;
    const fillConnections = (): void => {
      const names = connections();
      const current = cell.connection;
      clear(
        connectionPicker,
        h("option", { value: "" }, "(%sql_connect)"),
        ...names.map((n) => h("option", { value: n, selected: n === current }, n)),
        ...(current && !names.includes(current) ? [h("option", { value: current, selected: true }, `${current} (not found)`)] : []),
      );
    };
    const more = button("More actions", () => cellMenu(cell, more), { icon: "more", iconOnly: true, class: "ghost" });
    const hiddenNote = h("button", { type: "button", class: "link wb-output-hidden", onclick: () => toggleOutput(cell) }, "Show output");
    const element = h(
      "section",
      { class: `wb-cell kind-${data.kind}`, tabindex: -1, role: "listitem" },
      h(
        "div",
        { class: "wb-cell-head" },
        count,
        kindPicker,
        connectionPicker,
        h("span", { class: "spacer" }),
        button("Run cell", () => void run(cell), { icon: "play", iconOnly: true, class: "ghost", title: "Run cell (Ctrl+Enter)" }),
        button("Move up", () => moveCell(cell, -1), { icon: "up", iconOnly: true, class: "ghost" }),
        button("Move down", () => moveCell(cell, 1), { icon: "down", iconOnly: true, class: "ghost" }),
        more,
        button("Delete cell", () => removeCell(cell), { icon: "trash", iconOnly: true, class: "ghost" }),
      ),
      editorHost,
      view,
      area.element,
      hiddenNote,
    );
    const cell: Cell = { kind: data.kind, source: data.source, connection: data.connection, execution_count: data.execution_count, element, editor: null, area, view, count, kindPicker, connectionPicker, wantsFocus: false };
    self = cell;
    const sql = sqlCompleter(kernel, () => cell.connection);
    connectionPicker.hidden = data.kind !== "sql";
    fillConnections();
    connectionPicker.addEventListener("focus", fillConnections);
    connectionPicker.addEventListener("change", () => {
      cell.connection = connectionPicker.value || null;
      changed();
    });
    kindPicker.addEventListener("change", () => setKind(cell, kindPicker.value as CellKind));
    element.addEventListener("focusin", () => select(cell));
    element.addEventListener("dblclick", () => {
      if (cell.kind === "markdown" && element.classList.contains("rendered")) editMarkdown(cell);
    });
    void loadEditors().then((m) => {
      cell.editor = m.createCodeEditor(editorHost, {
        language: LANGUAGE[cell.kind],
        value: cell.source,
        theme: theme(),
        grow: true,
        placeholder: cell.kind === "sql" ? "SQL" : undefined,
        // By the cell's kind as it is now: Python from the kernel, SQL from the connection's catalog.
        complete: (text, offset) => (cell.kind === "sql" ? sql(text, offset) : cell.kind === "code" ? python(text, offset) : Promise.resolve([])),
        help: (text, offset) => (cell.kind === "code" ? kernel.help(text, offset) : Promise.resolve("")),
        sendToAgent: (what, ask) => sendToAgent(codeItem(`cell ${indexOf(cell) + 1} of ${currentDocument()}${what.startLine === what.endLine ? `, line ${what.startLine}` : `, lines ${what.startLine} to ${what.endLine}`}`, what, LANGUAGE[cell.kind]), ask),
        keys: {
          shiftEnter: () => void run(cell).then(() => next(cell)),
          ctrlEnter: () => void run(cell),
          escape: () => toCommandMode(cell),
          f1: hooks.help,
        },
      });
      cell.editor.onChange(changed);
      if (hooks.cursor) cell.editor.onCursor(hooks.cursor);
      if (cell.wantsFocus) focusEditor(cell);
    });
    if (cell.kind === "markdown" && cell.source.trim()) showMarkdown(cell);
    else area.set(data.outputs);
    cells.splice(at, 0, cell);
    list.insertBefore(element, list.children[at] ?? null);
    relabel();
    return cell;
  };

  /** The keyboard into a cell's editor: now if it exists, else as soon as it does. A user who clicked
   *  somewhere else in the meantime keeps their focus (a delayed focus once split typed text between
   *  two cells). */
  const focusEditor = (cell: Cell): void => {
    if (!cell.editor) {
      cell.wantsFocus = true;
      const asked = document.activeElement;
      const moved = (): void => {
        if (document.activeElement !== asked) cell.wantsFocus = false;
      };
      document.addEventListener("focusin", moved, { once: true });
      return;
    }
    if (cell.wantsFocus || !cell.editor.hasFocus()) {
      cell.wantsFocus = false;
      cell.editor.focus();
    }
  };

  const toCommandMode = (cell: Cell): void => {
    select(cell);
    cell.element.focus();
  };

  const edit = (cell: Cell): void => {
    select(cell);
    if (cell.kind === "markdown" && cell.element.classList.contains("rendered")) editMarkdown(cell);
    else focusEditor(cell);
  };

  const next = (cell: Cell): void => {
    const following = cells[indexOf(cell) + 1] ?? make(empty(cell.kind === "markdown" ? "code" : cell.kind, cell.connection), cells.length);
    select(following);
    following.element.scrollIntoView({ block: "nearest" });
    focusEditor(following);
  };

  const insertAt = (at: number, kind: CellKind = "code", source = ""): Cell => {
    const cell = make({ ...empty(kind, kind === "sql" ? (connections()[0] ?? null) : null), source }, Math.max(0, Math.min(at, cells.length)));
    select(cell);
    cell.element.scrollIntoView({ block: "nearest" });
    focusEditor(cell);
    changed();
    return cell;
  };

  const place = (cell: Cell, to: number): void => {
    cells.splice(indexOf(cell), 1);
    cells.splice(to, 0, cell);
    list.insertBefore(cell.element, cells[to + 1]?.element ?? null);
    relabel();
  };

  const moveCell = (cell: Cell, by: number): void => {
    const from = indexOf(cell);
    const to = from + by;
    if (to < 0 || to >= cells.length) return;
    place(cell, to);
    undos.push({ kind: "move", cell, from });
    cell.element.scrollIntoView({ block: "nearest" });
    changed();
  };

  const dataOf = (c: Cell): CellData => ({ kind: c.kind, source: c.editor ? c.editor.value() : c.source, connection: c.kind === "sql" ? c.connection : null, outputs: c.kind === "markdown" ? [] : c.area.outputs(), execution_count: c.execution_count });

  const removeCell = (cell: Cell): void => {
    const at = indexOf(cell);
    const data = dataOf(cell);
    cells.splice(at, 1);
    cell.editor?.dispose();
    cell.element.remove();
    // The last cell gone: an empty one in its place (Undo takes it away again if still empty).
    const filler = cells.length ? null : make(empty(), 0);
    undos.push({ kind: "delete", data, at, filler });
    if (undos.length > 100) undos.shift();
    const after = cells[Math.min(at, cells.length - 1)];
    if (after) {
      select(after);
      if (document.activeElement === document.body) after.element.focus();
    }
    relabel();
    changed();
    toast("Cell deleted.", "info", { label: "Undo", run: () => void undo() });
  };

  const undo = (): boolean => {
    const last = undos.pop();
    if (!last) return false;
    if (last.kind === "move") {
      if (cells.includes(last.cell)) place(last.cell, Math.min(last.from, cells.length - 1));
    } else {
      // The empty cell made when the last one went is replaced by the one coming back.
      const lone = last.filler && cells.includes(last.filler) && !(last.filler.editor?.value() ?? last.filler.source).trim() ? last.filler : null;
      const back = make(last.data, Math.min(last.at, cells.length));
      if (lone) {
        cells.splice(indexOf(lone), 1);
        lone.editor?.dispose();
        lone.element.remove();
        relabel();
      }
      select(back);
      back.element.scrollIntoView({ block: "nearest" });
    }
    changed();
    return true;
  };

  const duplicate = (cell: Cell): void => {
    const copy = make({ ...dataOf(cell), outputs: [], execution_count: null }, indexOf(cell) + 1);
    select(copy);
    changed();
  };

  const clearOutput = (cell: Cell): void => {
    cell.area.set([]);
    cell.execution_count = null;
    cell.count.textContent = "[ ]";
    cell.element.classList.remove("failed", "output-hidden");
    changed();
  };

  const toggleOutput = (cell: Cell): void => {
    cell.element.classList.toggle("output-hidden");
  };

  // Command mode: Jupyter's keys on the selected cell when the focus is on the cell, not its editor.
  list.addEventListener("keydown", (e) => {
    const target = e.target as HTMLElement;
    if (!target.classList.contains("wb-cell") || !selected) return;
    const cell = selected;
    const at = indexOf(cell);
    const key = e.key.toLowerCase();
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    let handled = true;
    if (e.key === "Enter" && e.shiftKey) void run(cell).then(() => next(cell));
    else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void run(cell);
    else if (e.key === "Enter" && plain) edit(cell);
    else if (plain && !e.shiftKey && key === "a") insertAt(at, "code");
    else if (plain && !e.shiftKey && key === "b") insertAt(at + 1, "code");
    else if (plain && key === "d") {
      if (Date.now() - pendingD < 800) {
        pendingD = 0;
        removeCell(cell);
      } else pendingD = Date.now();
    } else if (plain && key === "y") setKind(cell, "code");
    else if (plain && key === "m") setKind(cell, "markdown");
    else if (plain && key === "s") setKind(cell, "sql");
    else if (plain && key === "z") undo();
    else if (plain && (key === "j" || e.key === "ArrowDown")) {
      const below = cells[at + 1];
      if (below) toCommandMode(below);
    } else if (plain && (key === "k" || e.key === "ArrowUp")) {
      const above = cells[at - 1];
      if (above) toCommandMode(above);
    } else if (plain && key === "o") toggleOutput(cell);
    else handled = false;
    if (handled) {
      e.preventDefault();
      e.stopPropagation();
    }
  });

  for (const data of initial.length ? initial : [empty()]) make(data, cells.length);

  const at = (index?: number): Cell | null => (index === undefined ? selected ?? cells[0] ?? null : cells[index] ?? null);
  return {
    element: list,
    cells: () => cells.map(dataOf),
    add: (kind, source = "", first = false) => void insertAt(first ? 0 : selected ? indexOf(selected) + 1 : cells.length, kind, source),
    insert: (where, kind = "code", source = "") => {
      const here = selected ? indexOf(selected) : cells.length - 1;
      insertAt(where === "above" ? here : here + 1, kind, source);
    },
    runAll: () => runEach([...cells]),
    runCell: async (index) => {
      const cell = at(index);
      if (!cell) throw new Error(`There is no cell ${index === undefined ? "selected" : index + 1}.`);
      select(cell);
      return run(cell);
    },
    runAbove: () => runEach(selected ? cells.slice(0, indexOf(selected)) : []),
    runBelow: () => runEach(selected ? cells.slice(indexOf(selected)) : [...cells]),
    selected: () => (selected ? indexOf(selected) : -1),
    select: (index, editIt = false) => {
      const cell = cells[index];
      if (!cell) throw new Error(`There is no cell ${index + 1}: the notebook has ${cells.length}.`);
      cell.element.scrollIntoView({ block: "nearest" });
      if (editIt) edit(cell);
      else toCommandMode(cell);
    },
    remove: (index) => {
      const cell = at(index);
      if (cell) removeCell(cell);
    },
    move: (by) => {
      if (selected) moveCell(selected, by);
    },
    setKind: (kind) => {
      if (selected) setKind(selected, kind);
    },
    duplicate: () => {
      if (selected) duplicate(selected);
    },
    clearOutput: (index) => {
      const cell = at(index);
      if (cell) clearOutput(cell);
    },
    clearAll: () => {
      for (const c of cells) clearOutput(c);
    },
    toggleOutput: () => {
      if (selected) toggleOutput(selected);
    },
    commandMode: () => {
      if (selected) toCommandMode(selected);
    },
    undo,
    helpAtCursor: async () => {
      const cell = selected;
      if (!cell?.editor || cell.kind !== "code") return "";
      return kernel.help(cell.editor.value(), cell.editor.cursorOffset());
    },
    setTheme: (t) => {
      for (const c of cells) c.editor?.setTheme(t);
    },
  };
}
