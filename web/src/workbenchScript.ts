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
 * ROUTING: POST /workbench/statement {code, line}: the statement's lines
 */
import { api } from "./api";
import { h } from "./dom";
import type { CodeEditor } from "./lazy/codeEditor";
import { codeItem, currentDocument, sendToAgent } from "./workbenchAgent";
import { type ConsoleView, loadEditors, type RunOrigin } from "./workbenchConsole";
import { pythonCompleter } from "./workbenchCompletion";
import { type ErrorDetail, type Fix, type Frame, problemsFor, yourFrame } from "./workbenchErrors";
import type { Kernel } from "./workbenchKernel";
import type { ErrorHooks } from "./workbenchOutputs";

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
  hooks: { cursor?: (line: number, column: number) => void; help?: () => void; fix?: (fix: Fix) => boolean; openFile?: (path: string, line: number) => void; openGrid?: (name: string) => void } = {},
): ScriptView {
  const editorHost = h("div", { class: "wb-script-editor" });
  let editor: CodeEditor | null = null;
  let text = initial;

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
    await console.run({ code, kind: "file", path }, name ? `%run ${name}` : `# the script (${lines} line${lines === 1 ? "" : "s"}, not saved)`, { kind: "file", path });
  };

  /** The whole statement at the cursor, then the cursor on to the next (the selection's lines when
   *  there is one). The cursor moves at once: pressing again queues the next statement. */
  const runSelection = async (): Promise<void> => {
    if (!editor) return;
    const chosen = editor.selected();
    if (chosen) {
      const part = chosen.startLine === chosen.endLine && chosen.text.trim() !== chosen.wholeLines.trim();
      const code = part ? chosen.text.trim() : dedent(chosen.wholeLines);
      const indent = part ? Math.max(0, chosen.wholeLines.indexOf(code)) : sharedIndent(chosen.wholeLines);
      await console.run({ code }, code.trimEnd(), { kind: "lines", start: chosen.startLine, indent });
      return;
    }
    const line = editor.cursorLine();
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
    const code = to ? editor.lines(1, found.end ?? line) : editor.lines(found.start ?? line, last);
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
    });
    editor.onChange(() => {
      text = editor!.value();
      changed();
    });
    if (hooks.cursor) editor.onCursor(hooks.cursor);
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
  };
}
