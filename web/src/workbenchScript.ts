/**
 * workbenchScript.ts: a Python script in the Workbench: the editor, in the source pane; its runs show
 * in the document's console (workbenchConsole.ts), on the document's IPython kernel. [WIRE]
 *
 *   Run file (F5)        the file as Python would run it (`%run`: __name__ is "__main__", __file__ and
 *                        sys.argv are set, multiprocessing works), saved first when it has a path; an
 *                        unsaved script runs from a private copy
 *   Run statement        Ctrl+Enter, Shift+Enter or F9: the whole statement at the cursor (a `for`
 *                        block, a function, a call over several lines), then the cursor moves to the
 *                        next one (RStudio's run line); with a selection, its whole lines (a part of one
 *                        line runs as it is: an expression to look at)
 *   Run to here, from here   the script's top to the cursor's statement, or from it to the end
 *   Help (F1)            the name at the cursor, in the Help pane
 *
 *   [CELLS]    `# %%` starts a cell (VS Code, Spyder, PyCharm and Jupytext read it too); each cell has
 *              a number that never changes, on its marker (`# %% [7]`); `@cell7` in a comment links
 *              to it. The code before the first marker is the script's main code, not a cell
 *   [TABLES]   every table open in the grid has a read-only cell here (docs/ideas.md, 2026-11-14):
 *              `# %% [8] [transformations] orders`, the grid's steps as one DuckDB query. All tables
 *              sit at the top between `# region Tables` and `# endregion`, each with the user's cell
 *              that makes it (`# %% [7] [table] orders`); the editor folds the group, each table and
 *              each cell, and says each table's state beside its marker. Out of date is yellow;
 *              Ctrl+Enter in a table's cell runs it. Nothing runs by itself
 *   [USES]     a `# %%` cell that names a table starts with `# uses orders @cell8`, kept as they type
 *
 * ROUTING: POST /workbench/statement {code, line}: the statement's lines
 *          POST /workbench/tables/arrange {kind: "script", text, tables, move}: the text as it should
 *          be and where its cells are (the rules are the server's: domain/table_cells.py)
 */
import { api } from "./api";
import { h, toast } from "./dom";
import type { CodeEditor, LineNote, ScriptOutline } from "./lazy/codeEditor";
import { codeItem, currentDocument, sendToAgent } from "./workbenchAgent";
import { type ConsoleView, loadEditors, type RunOrigin } from "./workbenchConsole";
import { pythonCompleter } from "./workbenchCompletion";
import { type ErrorDetail, type Fix, type Frame, problemsFor, yourFrame } from "./workbenchErrors";
import type { Kernel } from "./workbenchKernel";
import type { TableReport } from "./workbenchNotebook";
import type { ErrorHooks } from "./workbenchOutputs";
import { groupLineText, type RunRecord, type TableInfo, type TableLine, tableLineText, tableState } from "./workbenchTables";

export interface ScriptView {
  element: HTMLElement;
  text: () => string;
  runFile: () => Promise<void>;
  runSelection: () => Promise<void>;
  runToHere: () => Promise<void>;
  runFromHere: () => Promise<void>;
  /** The help for the name at the cursor ("" when there is none). */
  helpAtCursor: () => Promise<string>;
  focus: () => void;
  /** The cursor to a line (a frame clicked in another document's error panel). */
  reveal: (line: number) => void;
  setTheme: (theme: string) => void;
  /** What a run's error panel does here: marks this script's lines, fixes them, goes to a frame. */
  errorHooks: (origin?: RunOrigin) => ErrorHooks;
  /** The open tables that belong to this script: their cells follow, the Tables group is arranged. */
  setTables: (tables: TableInfo[]) => void;
  holdsTable: (name: string) => boolean;
  tables: () => TableReport[];
  runTable: (name: string) => Promise<boolean>;
  runOutOfDate: () => Promise<number>;
  /** Go to the cell with this number (false: there is none). */
  goTo: (number: number) => boolean;
  foldTables: (folded: boolean, table?: string) => void;
  renameTable: (from: string, to: string) => void;
}

export interface ScriptFile {
  /** The file's path (null: never saved). */
  path: () => string | null;
  /** Save it before Run file when it has changes (false: not saved, so do not run). */
  saveFirst: () => Promise<boolean>;
}

/** The indent every non-blank line shares (what dedent takes off). */
export function sharedIndent(code: string): number {
  const indents = code.split("\n").filter((l) => l.trim()).map((l) => /^[ \t]*/.exec(l)![0].length);
  return indents.length ? Math.min(...indents) : 0;
}

/** Python's own dedent for lines run from inside a block: they lose their shared indent. */
export function dedent(code: string): string {
  const cut = sharedIndent(code);
  return code.split("\n").map((l) => l.slice(cut)).join("\n");
}

const sameFile = (a: string | null, b: string | null): boolean => !!a && !!b && a.replace(/\\/g, "/").toLowerCase() === b.replace(/\\/g, "/").toLowerCase();

interface Statement {
  start: number | null;
  end: number | null;
  next: number | null;
}

export function scriptView(
  initial: string,
  kernel: Kernel,
  console: ConsoleView,
  theme: () => string,
  changed: () => void,
  names: () => { connections: string[]; blades: string[]; codeNames?: { name: string; connection: string; kind: string }[] },
  file: ScriptFile,
  hooks: { cursor?: (line: number, column: number) => void; help?: () => void; fix?: (fix: Fix) => boolean; openFile?: (path: string, line: number) => void; openGrid?: (name: string) => void; openResult?: (count: number, name?: string) => void; tablesChanged?: () => void } = {},
): ScriptView {
  const editorHost = h("div", { class: "wb-script-editor" });
  let editor: CodeEditor | null = null;
  let text = initial;

  // ===== the Tables group: arranged by the server's rules, folded and drawn here =====
  let tableInfos: TableInfo[] = [];
  let tableLines: TableLine[] = [];
  let outline: ScriptOutline = { region: null, cells: [] };
  const records = new Map<string, RunRecord>();
  let version = 0; // counts every change: an arrangement worked out for an older text is dropped
  let asked = 0;
  let quiet: number | null = null;
  let applying = false;
  // The first look at a script only gives its cells their numbers: that does not count as a change
  // (the numbers come out the same the next time it opens; they are saved when the user saves).
  let quietly = true;
  // A file that came with a Tables group: it starts folded, and its tables have not run since.
  const cameWithGroup = /^# ?region Tables\s*$/m.test(initial);
  let foldOnce = cameWithGroup;
  const stateOf = (line: TableLine) => tableState(line.code, records.get(line.name));
  const variableOf = (name: string): string => tableInfos.find((t) => t.name === name)?.variable ?? name.replace(/\W/g, "_");
  /** The line each table of the group folds at: its maker's marker, or its own cell's. */
  const tableStarts = (): Map<string, number> => {
    const starts = new Map<string, number>();
    for (const cell of outline.cells) {
      if (cell.grouped && cell.table && (cell.role === "maker" || cell.role === "transformations") && !starts.has(cell.table)) starts.set(cell.table, cell.line);
    }
    return starts;
  };

  const draw = (): void => {
    if (!editor) return;
    const notes: LineNote[] = [];
    if (outline.region) {
      const group = groupLineText(tableLines.map(stateOf));
      notes.push({ line: outline.region[0], text: group.text.replace(/^Tables · /, ""), tone: group.stale ? "stale" : "plain", marker: true });
      notes.push({ line: outline.region[1], text: "", tone: "plain", marker: true });
      notes.push({ line: outline.region[1] + 1, text: "", tone: "plain", marker: true }); // the plain marker that closes the group
    }
    const starts = tableStarts();
    for (const cell of outline.cells) {
      const line = tableLines.find((l) => l.name === cell.table);
      const state = line ? stateOf(line) : null;
      let words = "";
      if (line && state && cell.role === "transformations") {
        const steps = starts.get(line.name) === cell.line || !cell.grouped ? `${tableLineText(line, state).replace(`${line.name} · `, "").replace(` · ${state.short}`, "")} · ` : "";
        words = `${steps}read-only: it follows the grid · ${state.label}${state.runnable ? " · Ctrl+Enter runs it" : ""}`;
      } else if (line && state && cell.role === "maker" && cell.grouped) words = tableLineText(line, state).replace(`${line.name} · `, "");
      notes.push({ line: cell.line, text: words, tone: state?.kind === "outOfDate" && cell.table ? "stale" : "plain", marker: true });
    }
    editor.setLineNotes(notes);
    hooks.tablesChanged?.();
  };

  const relevant = (): boolean => tableInfos.length > 0 || /^# ?(%%|region Tables)/m.test(editor ? editor.value() : text);

  /** Ask the server for the script as it should be, and where its cells are. `move` false while the
   *  user types: nothing is added or moved; only the tables' cells and the uses lines follow. */
  const arrange = async (move: boolean): Promise<void> => {
    if (!editor) return;
    if (!relevant()) {
      if (outline.cells.length || outline.region) {
        outline = { region: null, cells: [] };
        tableLines = [];
        editor.setOutline(null);
        draw();
      }
      return;
    }
    const mine = ++asked;
    const at = version;
    try {
      const done = await api<{ text: string; tables: TableLine[]; outline: ScriptOutline }>("/workbench/tables/arrange", "POST", { kind: "script", text: editor.value(), tables: tableInfos.map((t) => t.name), move });
      if (mine !== asked) return; // a newer question is on its way
      if (at !== version) return void (move ? arrange(true) : typedSoon()); // typed meanwhile: ask again
      applying = true;
      quietly = quietly && tableInfos.length === 0;
      try {
        editor.replaceText(done.text);
      } finally {
        applying = false;
        quietly = false;
      }
      outline = done.outline;
      tableLines = done.tables;
      editor.setOutline(outline);
      draw();
      if (foldOnce && outline.region) {
        foldOnce = false;
        // Once Monaco has the folds: each table, then the group (a file's group starts folded).
        setTimeout(() => {
          editor?.fold([...tableStarts().values()]);
          if (outline.region) editor?.fold([outline.region[0]]);
        }, 600);
      }
    } catch {
      /* the server is away: the text stays as it is */
    }
  };

  function typedSoon(): void {
    if (quiet !== null) window.clearTimeout(quiet);
    quiet = window.setTimeout(() => {
      quiet = null;
      void arrange(false);
    }, 600);
  }

  /** The table whose read-only cell this line is in (its marker or its code). */
  const tableAt = (line: number): string | null => outline.cells.find((c) => c.role === "transformations" && line >= c.line && line <= c.end)?.table ?? null;

  /** Lines of the script as they run: a table's cell inside them goes through the kernel's safe run
   *  (client/table_cells.py), on the same lines, so it never transforms a transformed table. */
  const safeLines = (start: number, end: number): string => {
    if (!editor) return "";
    const lines = editor.lines(start, end).split("\n");
    for (const cell of outline.cells) {
      if (cell.role !== "transformations" || !cell.table || cell.line + 1 < start || cell.end > end || cell.end <= cell.line) continue;
      const from = cell.line + 1 - start;
      const to = cell.end - start;
      const body = lines.slice(from, to + 1).join("\n");
      if (!body.trim()) continue;
      lines[from] = `__import__('swissarmygrid.client.table_cells', fromlist=['run']).run(${JSON.stringify(variableOf(cell.table))}, ${JSON.stringify(body)})`;
      for (let k = from + 1; k <= to; k++) lines[k] = "";
    }
    return lines.join("\n");
  };

  const runTable = async (name: string): Promise<boolean> => {
    const line = tableLines.find((l) => l.name === name);
    if (!line) throw new Error(`This script has no table named ${name}.`);
    const ok = await console.run({ code: line.code, kind: "table", table: variableOf(name) }, `# ${name}: its transformations`);
    if (ok) records.set(name, { code: line.code, at: new Date(), opened: false });
    draw();
    return ok;
  };

  const goTo = (number: number): boolean => {
    const cell = outline.cells.find((c) => c.number === number);
    if (!cell || !editor) {
      toast(`There is no cell ${number} in this script.`, "error");
      return false;
    }
    if (cell.grouped && outline.region) editor.unfold([outline.region[0], cell.line]);
    editor.reveal(cell.line);
    return true;
  };

  const statementAt = async (line: number): Promise<Statement> => {
    try {
      return await api<Statement>("/workbench/statement", "POST", { code: editor ? editor.value() : text, line });
    } catch {
      return { start: line, end: line, next: line + 1 };
    }
  };

  const runFile = async (): Promise<void> => {
    const code = editor ? editor.value() : text;
    if (!code.trim()) return console.note("Empty script");
    if (!(await file.saveFirst())) return;
    const path = file.path();
    const name = path ? (path.split(/[\\/]/).pop() ?? path) : null;
    const lines = code.split("\n").length;
    const ran = tableLines.map((l) => ({ name: l.name, code: l.code }));
    const ok = await console.run({ code, kind: "file", path }, name ? `%run ${name}` : `# the script (${lines} line${lines === 1 ? "" : "s"}, not saved)`, { kind: "file", path });
    // The file ran to its end: so did its tables' cells (each the safe way, in the kernel).
    if (ok) for (const table of ran) records.set(table.name, { code: table.code, at: new Date(), opened: false });
    draw();
    if (ok) void arrange(true);
  };

  /** The whole statement at the cursor, then the cursor on to the next (the selection's lines when
   *  there is one). The cursor moves at once: pressing again queues the next statement. */
  const runSelection = async (): Promise<void> => {
    if (!editor) return;
    const chosen = editor.selected();
    if (chosen) {
      const part = chosen.startLine === chosen.endLine && chosen.text.trim() !== chosen.wholeLines.trim();
      const code = part ? chosen.text.trim() : dedent(safeLines(chosen.startLine, chosen.endLine));
      const indent = part ? Math.max(0, chosen.wholeLines.indexOf(code)) : sharedIndent(chosen.wholeLines);
      await console.run({ code }, code.trimEnd(), { kind: "lines", start: chosen.startLine, indent });
      return;
    }
    const line = editor.cursorLine();
    // In a table's read-only cell: its Run.
    const table = tableAt(line);
    if (table) return void (await runTable(table));
    const found = await statementAt(line);
    if (found.start === null || found.end === null) {
      // A blank line or a comment: on to the next line with code.
      const lines = editor.value().split("\n");
      let next = line + 1;
      while (next <= lines.length && (!lines[next - 1]!.trim() || lines[next - 1]!.trim().startsWith("#"))) next++;
      editor.moveTo(Math.min(next, lines.length));
      return;
    }
    const block = editor.lines(found.start, found.end);
    const code = dedent(block);
    editor.moveTo(found.next ?? Math.min(found.end + 1, editor.lineCount()));
    await console.run({ code }, code.trimEnd(), { kind: "lines", start: found.start, indent: sharedIndent(block) });
  };

  /** The script's top to the end of the cursor's statement, or from its start to the end. */
  const runPart = async (to: boolean): Promise<void> => {
    if (!editor) return;
    const line = editor.cursorLine();
    const found = await statementAt(line);
    const last = editor.lineCount();
    const code = to ? safeLines(1, found.end ?? line) : safeLines(found.start ?? line, last);
    if (!code.trim()) return console.note("Nothing to run");
    await console.run({ code }, to ? `# lines 1 to ${found.end ?? line}` : `# lines ${found.start ?? line} to ${last}`, { kind: "lines", start: to ? 1 : (found.start ?? line), indent: 0 });
  };

  void loadEditors().then((m) => {
    editor = m.createCodeEditor(editorHost, {
      language: "python",
      value: initial,
      theme: theme(),
      keys: { f5: () => void runFile(), ctrlEnter: () => void runSelection(), shiftEnter: () => void runSelection(), f1: hooks.help },
      complete: pythonCompleter(kernel, names),
      help: (code, offset) => kernel.help(code, offset),
      sendToAgent: (what, ask) =>
        sendToAgent(codeItem(`${currentDocument()}, ${what.startLine === what.endLine ? `line ${what.startLine}` : `lines ${what.startLine} to ${what.endLine}`}`, what, "python"), ask),
      cellLinks: { first: (n) => outline.cells.find((c) => c.number === n)?.first ?? null, go: (n) => void goTo(n) },
    });
    editor.onChange((change) => {
      text = editor!.value();
      version++;
      if (!(applying && quietly)) changed();
      // The uses lines and the cells' places follow the typing, after a short pause (never an undo).
      if (!applying && !change.undo) typedSoon();
    });
    if (hooks.cursor) editor.onCursor(hooks.cursor);
    // A table's cell is read-only: it follows the grid.
    editor.onCursor((line) => editor!.setReadOnly(outline.cells.some((c) => c.role === "transformations" && line > c.line && line <= c.end)));
    if (cameWithGroup) for (const match of initial.matchAll(/^# ?%%(?: \[\d+\])? \[transformations\] ([A-Za-z_][\w-]*)/gm)) records.set(match[1]!, { code: null, at: null, opened: true });
    void arrange(true);
  });

  /** A frame of this run, as this script's line and the columns the dedent took off (null: elsewhere). */
  const placeIn = (origin: RunOrigin | undefined, detail: ErrorDetail) => (frame: Pick<Frame, "where" | "cell" | "file" | "line">): { line: number; colShift: number } | null => {
    if (!origin) return null;
    if (origin.kind === "file") {
      const path = origin.path ?? file.path();
      const mine = frame.where === "file" && (sameFile(frame.file, path) || (!origin.path && /[\\/]untitled\.py$/.test(frame.file ?? "")));
      return mine ? { line: frame.line, colShift: 0 } : null;
    }
    const numbers = detail.chain.flatMap((e) => e.frames.map((f) => f.cell ?? 0).concat(e.syntax?.cell ?? 0));
    return frame.where === "cell" && frame.cell === Math.max(...numbers) ? { line: origin.start + frame.line - 1, colShift: origin.indent } : null;
  };

  const errorHooks = (origin?: RunOrigin): ErrorHooks => {
    let shown: ErrorDetail | null = null;
    const yours = (): { line: number; colShift: number } | null => {
      const entry = shown?.chain[shown.chain.length - 1];
      const frame = yourFrame(entry) ?? (entry?.syntax ? ({ where: entry.syntax.where, cell: entry.syntax.cell, file: entry.syntax.file, line: entry.syntax.line } as Frame) : null);
      return frame && shown ? placeIn(origin, shown)(frame) : null;
    };
    return {
      shown: (detail) => {
        shown = detail;
        editor?.setProblems(problemsFor(detail, placeIn(origin, detail)));
      },
      fix: (fix) => {
        if (fix.kind === "replace" || fix.kind === "import") {
          if (!editor?.applyFix(fix, fix.kind === "import" ? 1 : (yours()?.line ?? editor.cursorLine()))) console.note(`${fix.target ?? "That"} is no longer in the script.`);
          else editor.focus(); // back in the code, ready to run it again
          return;
        }
        if (!hooks.fix?.(fix)) console.note(fix.kind === "define" ? `Nothing defines ${fix.name}` : "Fix not available here");
      },
      goTo: (frame) => {
        const at = shown ? placeIn(origin, shown)(frame) : null;
        if (at && editor) {
          editor.reveal(at.line, (frame.col ?? 1) + at.colShift);
          return true;
        }
        if (frame.where === "file" && frame.file && hooks.openFile) {
          hooks.openFile(frame.file, frame.line);
          return true;
        }
        return false;
      },
      openGrid: hooks.openGrid,
      openResult: hooks.openResult,
    };
  };

  return {
    errorHooks,
    element: h("div", { class: "wb-script" }, editorHost),
    text: () => (editor ? editor.value() : text),
    runFile,
    runSelection,
    runToHere: () => runPart(true),
    runFromHere: () => runPart(false),
    helpAtCursor: async () => (editor ? kernel.help(editor.value(), editor.cursorOffset()) : ""),
    focus: () => editor?.focus(),
    reveal: (line) => editor?.reveal(line),
    setTheme: (t) => editor?.setTheme(t),
    setTables: (tables) => {
      tableInfos = tables;
      void arrange(true);
    },
    holdsTable: (name) => tableLines.some((l) => l.name === name) || new RegExp(`^# ?%%(?: \\[\\d+\\])? \\[transformations\\] ${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "m").test(editor ? editor.value() : text),
    tables: () =>
      tableLines.map((line) => {
        const state = stateOf(line);
        return { ...line, state: state.kind, label: state.label };
      }),
    runTable,
    runOutOfDate: async () => {
      let ran = 0;
      for (const line of [...tableLines]) {
        if (!stateOf(line).runnable) continue;
        ran++;
        if (!(await runTable(line.name))) break;
      }
      return ran;
    },
    goTo,
    foldTables: (folded, table) => {
      if (!editor || !outline.region) return;
      const start = table ? tableStarts().get(table) : outline.region[0];
      if (start === undefined) return;
      if (folded) editor.fold([start]);
      else editor.unfold(table ? [outline.region[0], start] : [start]);
    },
    renameTable: (from, to) => {
      const record = records.get(from);
      if (record) records.set(to, record);
      records.delete(from);
      // Its markers carry the name: `# %% [8] [transformations] orders`.
      const marker = new RegExp(`^(# ?%%(?: \\[\\d+\\])? \\[(?:table|transformations)\\] )${from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w-])`, "gm");
      if (editor) editor.replaceText(editor.value().replace(marker, `$1${to}`));
    },
  };
}
