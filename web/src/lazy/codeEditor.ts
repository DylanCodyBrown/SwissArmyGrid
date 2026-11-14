/**
 * lazy/codeEditor.ts: the Monaco editor for the Workbench (a notebook's cells, a script). [WIRE]
 *
 * Python, SQL or Markdown; the theme follows the page. A cell editor grows with its text (no
 * scrollbar of its own); a script's fills its pane. Drop text on it (a connection's line, a table's
 * name from the left panel) and it lands where it was dropped: Monaco's own drop.
 */
import * as monaco from "monaco-editor";

(self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
  getWorker: () => new Worker("/static/build/editor.worker.js", { type: "module" }),
};

const THEMES: Record<string, string> = { light: "vs", dark: "vs-dark", "hc-light": "hc-light", "hc-dark": "hc-black" };

/** A completion: what it shows, what it inserts, its kind (variable, function, table, column...). */
export interface Suggestion {
  label: string;
  insert?: string;
  kind?: string;
  detail?: string;
  /** Where the insert goes, as offsets into the text (default: the word at the cursor). */
  start?: number;
  end?: number;
}
export type Completer = (text: string, offset: number) => Promise<Suggestion[]>;

/** A place a run's error went through (workbenchErrors.ts): a squiggle, a message at the line's end,
 *  a hover card, and quick fixes (Ctrl+.). Lines and columns 1-based; a null column marks the line. */
export interface Problem {
  line: number;
  col: number | null;
  endLine: number;
  endCol: number | null;
  message: string;
  /** Markdown for the hover card (escaped: no HTML). */
  hover: string;
  severity: "error" | "info";
  fixes: { kind: string; title: string; target?: string; text?: string; line?: string }[];
}
export type Helper = (text: string, offset: number) => Promise<string>;

// Each editor's own completer and help (a notebook cell's kernel and connection, a script's kernel).
const completers = new WeakMap<monaco.editor.ITextModel, { complete?: Completer; help?: Helper }>();
// Each editor's problems from its last failed run (cleared by the next edit or run).
const problems = new WeakMap<monaco.editor.ITextModel, Problem[]>();
const PROBLEMS = "sag-run";

function rangeOf(model: monaco.editor.ITextModel, p: Problem): monaco.Range {
  const last = Math.min(Math.max(p.endLine, p.line), model.getLineCount());
  const line = Math.min(p.line, model.getLineCount());
  const start = p.col ?? model.getLineFirstNonWhitespaceColumn(line) ?? 1;
  const end = p.endCol ?? model.getLineMaxColumn(last);
  return new monaco.Range(line, Math.max(1, start), last, Math.max(start + (line === last ? 1 : 0), end));
}
const registered = new Set<string>();
let editorCount = 0;
const KINDS: Record<string, monaco.languages.CompletionItemKind> = {
  function: monaco.languages.CompletionItemKind.Function,
  method: monaco.languages.CompletionItemKind.Method,
  class: monaco.languages.CompletionItemKind.Class,
  module: monaco.languages.CompletionItemKind.Module,
  instance: monaco.languages.CompletionItemKind.Variable,
  statement: monaco.languages.CompletionItemKind.Variable,
  keyword: monaco.languages.CompletionItemKind.Keyword,
  param: monaco.languages.CompletionItemKind.Variable,
  path: monaco.languages.CompletionItemKind.File,
  property: monaco.languages.CompletionItemKind.Property,
  table: monaco.languages.CompletionItemKind.Struct,
  view: monaco.languages.CompletionItemKind.Interface,
  schema: monaco.languages.CompletionItemKind.Folder,
  column: monaco.languages.CompletionItemKind.Field,
  connection: monaco.languages.CompletionItemKind.Reference,
};

/** One completion and one hover provider per language, asking the editor's own completer. */
function register(language: string): void {
  if (registered.has(language)) return;
  registered.add(language);
  monaco.languages.registerCompletionItemProvider(language, {
    triggerCharacters: [".", "\"", "'"],
    provideCompletionItems: async (model, position) => {
      const own = completers.get(model)?.complete;
      if (!own) return { suggestions: [] };
      const offset = model.getOffsetAt(position);
      const found = await own(model.getValue(), offset);
      const word = model.getWordUntilPosition(position);
      const wordRange = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
      return {
        // Asked again as typing goes on: the kernel's list changes with what is typed (a DataFrame's
        // columns come once a letter follows the dot), and it answers in well under a tenth of a second.
        incomplete: true,
        suggestions: found.map((s, i) => {
          const range = s.start !== undefined ? monaco.Range.fromPositions(model.getPositionAt(s.start), model.getPositionAt(s.end ?? offset)) : wordRange;
          return {
            label: s.label,
            insertText: s.insert ?? s.label,
            kind: KINDS[s.kind ?? ""] ?? monaco.languages.CompletionItemKind.Text,
            detail: s.detail ?? s.kind,
            range,
            sortText: String(i).padStart(5, "0"), // the order the completer gave
          };
        }),
      };
    },
  });
  monaco.languages.registerHoverProvider(language, {
    provideHover: async (model, position) => {
      // A problem under the pointer: its card first (Monaco's own marker hover sits above it).
      const here = (problems.get(model) ?? []).find((p) => rangeOf(model, p).containsPosition(position));
      if (here) return { range: rangeOf(model, here), contents: [{ value: here.hover, supportHtml: false }] };
      const help = completers.get(model)?.help;
      if (!help) return null;
      const text = await help(model.getValue(), model.getOffsetAt(position));
      return text ? { contents: [{ value: "```\n" + text.replace(/```/g, "ˋˋˋ") + "\n```" }] } : null;
    },
  });
  // Quick fixes (Ctrl+., the light bulb, the hover's "Quick Fix…"): a close name, an import.
  monaco.languages.registerCodeActionProvider(language, {
    provideCodeActions: (model, range) => {
      const actions: monaco.languages.CodeAction[] = [];
      for (const p of problems.get(model) ?? []) {
        const at = rangeOf(model, p);
        if (!at.intersectRanges(range) && at.startLineNumber !== range.startLineNumber) continue;
        for (const fix of p.fixes) {
          const edit = fixEdit(model, at, fix);
          if (!edit) continue;
          actions.push({ title: fix.title.replace(/`/g, ""), kind: "quickfix", isPreferred: actions.length === 0, edit: { edits: [{ resource: model.uri, textEdit: edit, versionId: model.getVersionId() }] } });
        }
      }
      return { actions, dispose: () => undefined };
    },
  });
}

/** The text edit a fix makes: the close name in place of the wrong one on the failing line(s), or an
 *  import line at the top. */
function fixEdit(model: monaco.editor.ITextModel, at: monaco.Range, fix: Problem["fixes"][number]): monaco.languages.TextEdit | null {
  if (fix.kind === "import" && fix.line) return { range: new monaco.Range(1, 1, 1, 1), text: `${fix.line}\n` };
  if (fix.kind !== "replace" || !fix.target || fix.text === undefined) return null;
  const pattern = new RegExp(`(?<![\\w])${fix.target.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w])`);
  for (let line = at.startLineNumber; line <= at.endLineNumber; line++) {
    const text = model.getLineContent(line);
    const found = pattern.exec(text);
    if (found) return { range: new monaco.Range(line, found.index + 1, line, found.index + 1 + fix.target.length), text: fix.text };
  }
  return null;
}

export interface CodeEditor {
  value: () => string;
  setValue: (text: string) => void;
  /** The selected text, or the cursor's line when nothing is selected. */
  selection: () => string;
  /** What is selected, null when nothing is: its text, and its first and last line (1-based). */
  selected: () => { text: string; startLine: number; endLine: number; wholeLines: string } | null;
  cursorLine: () => number;
  lineCount: () => number;
  /** Lines `start` to `end` (1-based, both included). */
  lines: (start: number, end: number) => string;
  /** The cursor to the start of a line, scrolled into view. */
  moveTo: (line: number) => void;
  /** A new line as Enter makes it (keeping the indent). */
  insertNewline: () => void;
  /** The cursor at the very end of the text. */
  atEnd: () => boolean;
  /** The cursor's offset into the text. */
  cursorOffset: () => number;
  /** Told the cursor's line and column whenever it moves. */
  onCursor: (listener: (line: number, column: number) => void) => void;
  /** Gets the keyboard focus (a notebook knows which cell is being edited). */
  onFocus: (listener: () => void) => void;
  hasFocus: () => boolean;
  /** Show a failed run's problems (squiggles, end-of-line messages); [] clears them. */
  setProblems: (list: Problem[]) => void;
  /** Make a fix's edit (replace a name, add an import); false when it no longer applies. */
  applyFix: (fix: Problem["fixes"][number], near: number) => boolean;
  /** The cursor to a line and column, scrolled into view, with the focus. */
  reveal: (line: number, column?: number) => void;
  setLanguage: (language: "python" | "sql" | "markdown") => void;
  focus: () => void;
  setTheme: (theme: string) => void;
  onChange: (listener: () => void) => void;
  layout: () => void;
  dispose: () => void;
}

export interface CodeEditorOptions {
  language: "python" | "sql" | "markdown";
  value: string;
  theme: string;
  /** Grow with the text (a notebook cell); otherwise fill the host (a script). */
  grow?: boolean;
  /** Keys: Shift+Enter, Ctrl+Enter (and F9), F5. Enter, Up and Down (the console): taken only with
   *  no suggestion list open; Up and Down answer false to move the cursor as usual. */
  keys?: {
    shiftEnter?: () => void;
    ctrlEnter?: () => void;
    f5?: () => void;
    escape?: () => void;
    enter?: () => void;
    up?: () => boolean;
    down?: () => boolean;
    /** F1: help on the name at the cursor (instead of Monaco's own command list). */
    f1?: () => void;
  };
  placeholder?: string;
  /** Completions at the cursor (the kernel, a connection's catalog), and help on hover. */
  complete?: Completer;
  help?: Helper;
}

export function createCodeEditor(host: HTMLElement, options: CodeEditorOptions): CodeEditor {
  const editor = monaco.editor.create(host, {
    value: options.value,
    language: options.language,
    theme: THEMES[options.theme] ?? "vs",
    automaticLayout: !options.grow,
    minimap: { enabled: false },
    lineNumbers: options.grow ? "off" : "on",
    glyphMargin: false,
    folding: !options.grow,
    lineDecorationsWidth: options.grow ? 6 : 10,
    scrollBeyondLastLine: false,
    renderLineHighlight: options.grow ? "none" : "line",
    overviewRulerLanes: 0,
    hideCursorInOverviewRuler: true,
    scrollbar: options.grow ? { vertical: "hidden", horizontal: "auto", alwaysConsumeMouseWheel: false, handleMouseWheel: false } : {},
    wordWrap: options.language === "markdown" ? "on" : "off",
    fontSize: 13,
    tabSize: 4,
    insertSpaces: true,
    dropIntoEditor: { enabled: true },
    // Suggestions as you type, inside strings too (a connection's name in sag.connection("…"), SQL in a string).
    quickSuggestions: { other: true, comments: false, strings: true },
    wordBasedSuggestions: "currentDocument",
    fixedOverflowWidgets: true,
    placeholder: options.placeholder,
  });
  const model = editor.getModel();
  if (model && (options.complete || options.help)) {
    completers.set(model, { complete: options.complete, help: options.help });
    for (const language of ["python", "sql", "markdown"]) register(language);
  }
  const keys = options.keys ?? {};
  // The run keys come first, even with the suggestion list open (it would take Ctrl+Enter for itself):
  // the list closes and the code runs.
  editor.onKeyDown((e) => {
    const run =
      e.keyCode === monaco.KeyCode.Enter && e.shiftKey && !e.ctrlKey && !e.metaKey ? keys.shiftEnter
      : e.keyCode === monaco.KeyCode.Enter && (e.ctrlKey || e.metaKey) && !e.shiftKey ? keys.ctrlEnter
      : e.keyCode === monaco.KeyCode.F9 ? keys.ctrlEnter
      : undefined;
    if (!run) return;
    e.preventDefault();
    e.stopPropagation();
    editor.trigger("keyboard", "hideSuggestWidget", null);
    run();
  });
  // Monaco's addCommand goes into one keybinding service shared by every editor on the page: without a
  // key of this editor's own in the condition, the console's Enter ran in a notebook cell (the newlines
  // typed there vanished), and the last cell's Escape answered for every cell.
  const own = `sagEditor${++editorCount}`;
  editor.createContextKey(own, true);
  if (keys.f5) editor.addCommand(monaco.KeyCode.F5, keys.f5, own);
  if (keys.f1) editor.addCommand(monaco.KeyCode.F1, keys.f1, own);
  if (keys.enter) editor.addCommand(monaco.KeyCode.Enter, keys.enter, `${own} && !suggestWidgetVisible`);
  if (keys.up) editor.addCommand(monaco.KeyCode.UpArrow, () => { if (!keys.up!()) editor.trigger("keyboard", "cursorUp", null); }, `${own} && !suggestWidgetVisible`);
  if (keys.down) editor.addCommand(monaco.KeyCode.DownArrow, () => { if (!keys.down!()) editor.trigger("keyboard", "cursorDown", null); }, `${own} && !suggestWidgetVisible`);
  if (keys.escape) editor.addCommand(monaco.KeyCode.Escape, keys.escape, `${own} && !suggestWidgetVisible`);

  // A failed run's marks, gone as soon as the code is edited (they describe the code that ran).
  const lens = editor.createDecorationsCollection([]);
  editor.onDidChangeModelContent(() => {
    const model = editor.getModel();
    if (model && problems.get(model)?.length) {
      problems.set(model, []);
      monaco.editor.setModelMarkers(model, PROBLEMS, []);
      lens.clear();
    }
  });

  const fit = (): void => {
    if (!options.grow) return;
    const height = Math.max(editor.getContentHeight(), 22);
    host.style.height = `${height}px`;
    editor.layout({ width: host.clientWidth, height });
  };
  if (options.grow) {
    editor.onDidContentSizeChange(fit);
    new ResizeObserver(() => editor.layout({ width: host.clientWidth, height: host.clientHeight })).observe(host);
    fit();
  }
  return {
    value: () => editor.getValue(),
    setValue: (text) => editor.setValue(text),
    selection: () => {
      const model = editor.getModel();
      const chosen = editor.getSelection();
      if (!model || !chosen) return "";
      if (!chosen.isEmpty()) return model.getValueInRange(chosen);
      return model.getLineContent(chosen.startLineNumber);
    },
    selected: () => {
      const model = editor.getModel();
      const chosen = editor.getSelection();
      if (!model || !chosen || chosen.isEmpty()) return null;
      // A selection ending at the start of a line (dragged down past a line's end) leaves that line out.
      const endLine = chosen.endColumn === 1 && chosen.endLineNumber > chosen.startLineNumber ? chosen.endLineNumber - 1 : chosen.endLineNumber;
      const wholeLines = model.getValueInRange(new monaco.Range(chosen.startLineNumber, 1, endLine, model.getLineMaxColumn(endLine)));
      return { text: model.getValueInRange(chosen), startLine: chosen.startLineNumber, endLine, wholeLines };
    },
    cursorLine: () => editor.getPosition()?.lineNumber ?? 1,
    lineCount: () => editor.getModel()?.getLineCount() ?? 1,
    lines: (start, end) => {
      const model = editor.getModel();
      if (!model) return "";
      const last = Math.min(end, model.getLineCount());
      return model.getValueInRange(new monaco.Range(start, 1, last, model.getLineMaxColumn(last)));
    },
    moveTo: (line) => {
      const last = editor.getModel()?.getLineCount() ?? 1;
      const at = Math.max(1, Math.min(line, last));
      editor.setPosition({ lineNumber: at, column: 1 });
      editor.revealLineInCenterIfOutsideViewport(at);
    },
    insertNewline: () => editor.trigger("keyboard", "type", { text: "\n" }),
    atEnd: () => {
      const model = editor.getModel();
      const at = editor.getPosition();
      if (!model || !at) return true;
      const last = model.getLineCount();
      return at.lineNumber === last && at.column === model.getLineMaxColumn(last);
    },
    cursorOffset: () => {
      const model = editor.getModel();
      const at = editor.getPosition();
      return model && at ? model.getOffsetAt(at) : 0;
    },
    onCursor: (listener) => void editor.onDidChangeCursorPosition((e) => listener(e.position.lineNumber, e.position.column)),
    onFocus: (listener) => void editor.onDidFocusEditorText(listener),
    hasFocus: () => editor.hasTextFocus(),
    setProblems: (list) => {
      const model = editor.getModel();
      if (!model) return;
      problems.set(model, list);
      monaco.editor.setModelMarkers(
        model,
        PROBLEMS,
        list.map((p) => {
          const r = rangeOf(model, p);
          return { severity: p.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Info, message: p.message, source: "Python", startLineNumber: r.startLineNumber, startColumn: r.startColumn, endLineNumber: r.endLineNumber, endColumn: r.endColumn };
        }),
      );
      // The message at the end of the failing line (Error Lens), and the line itself marked.
      lens.set(
        list.flatMap((p) => {
          const line = Math.min(Math.max(p.endLine, p.line), model.getLineCount());
          const end = model.getLineMaxColumn(line);
          return [
            { range: new monaco.Range(Math.min(p.line, model.getLineCount()), 1, line, 1), options: { isWholeLine: true, className: p.severity === "error" ? "wb-problem-line" : "wb-call-line" } },
            {
              // On the line's last character (an empty range shows no text after it).
              range: new monaco.Range(line, Math.max(1, end - 1), line, end),
              options: { showIfCollapsed: true, after: { content: `   ${p.message.split("\n")[0]!.slice(0, 160)}`, inlineClassName: p.severity === "error" ? "wb-problem-lens" : "wb-call-lens" } },
            },
          ];
        }),
      );
    },
    applyFix: (fix, near) => {
      const model = editor.getModel();
      if (!model) return false;
      const line = Math.min(Math.max(1, near), model.getLineCount());
      const edit = fixEdit(model, new monaco.Range(line, 1, line, model.getLineMaxColumn(line)), fix) ?? (fix.kind === "replace" ? fixEdit(model, model.getFullModelRange(), fix) : null);
      if (!edit) return false;
      editor.executeEdits("sag-fix", [{ range: monaco.Range.lift(edit.range), text: edit.text }]);
      return true;
    },
    reveal: (line, column = 1) => {
      editor.setPosition({ lineNumber: line, column });
      editor.revealLineInCenterIfOutsideViewport(line);
      editor.focus();
    },
    setLanguage: (language) => {
      const model = editor.getModel();
      if (model) monaco.editor.setModelLanguage(model, language);
      editor.updateOptions({ wordWrap: language === "markdown" ? "on" : "off" });
    },
    focus: () => editor.focus(),
    setTheme: (theme) => monaco.editor.setTheme(THEMES[theme] ?? "vs"),
    onChange: (listener) => void editor.onDidChangeModelContent(listener),
    layout: () => (options.grow ? fit() : editor.layout()),
    dispose: () => {
      editor.getModel()?.dispose();
      editor.dispose();
    },
  };
}
