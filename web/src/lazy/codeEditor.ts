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

/** `@cell7` in a comment links to cell 7: what the editor asks of its document. */
export interface CellLinks {
  /** The cell's first line of code (null: there is no such cell). */
  first: (number: number) => string | null;
  go: (number: number) => void;
}

/** A script's cells, for folding and drawing (domain/table_cells.py outline). Lines are 1-based. */
export interface ScriptOutline {
  region: [number, number] | null;
  cells: { number: number | null; line: number; end: number; role: string; table: string | null; grouped: boolean; first: string }[];
}

/** What a script's editor draws beside its Tables group: the words after a line, and its colour. */
export interface LineNote {
  line: number;
  text: string;
  /** stale: out of date (yellow). */
  tone: "plain" | "stale";
  /** Draw the line as a cell's marker. */
  marker?: boolean;
}

// Each editor's own completer and help (a notebook cell's kernel and connection, a script's kernel).
const completers = new WeakMap<monaco.editor.ITextModel, { complete?: Completer; help?: Helper }>();
const cellLinks = new WeakMap<monaco.editor.ITextModel, CellLinks>();
const outlines = new WeakMap<monaco.editor.ITextModel, ScriptOutline>();
const CELL_LINK = /@cell(\d+)\b/g;

/** Where a line's comment starts (0-based column; -1: it has none). Python's `#`, SQL's `--`; a
 *  Markdown cell is all words. A `#` inside a quoted text on the line is not a comment. */
function commentStart(line: string, language: string): number {
  if (language === "markdown") return 0;
  const mark = language === "sql" ? "--" : "#";
  let quote = "";
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]!;
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === quote) quote = "";
    } else if (ch === '"' || ch === "'") quote = ch;
    else if (line.startsWith(mark, i)) return i;
  }
  return -1;
}

/** The cell links on a line: each one's number and columns (1-based, end exclusive). */
function linksOn(line: string, language: string): { number: number; start: number; end: number }[] {
  const from = commentStart(line, language);
  if (from < 0) return [];
  const found: { number: number; start: number; end: number }[] = [];
  for (const match of line.matchAll(CELL_LINK)) {
    if (match.index >= from) found.push({ number: Number(match[1]), start: match.index + 1, end: match.index + 1 + match[0].length });
  }
  return found;
}

function linkAt(model: monaco.editor.ITextModel, position: monaco.IPosition): { number: number; range: monaco.Range } | null {
  if (!cellLinks.has(model)) return null;
  const hit = linksOn(model.getLineContent(position.lineNumber), model.getLanguageId()).find((l) => position.column >= l.start && position.column <= l.end);
  return hit ? { number: hit.number, range: new monaco.Range(position.lineNumber, hit.start, position.lineNumber, hit.end) } : null;
}

// Fired when a script's outline changes: Monaco asks for the folds again.
const foldsChanged = new monaco.Emitter<monaco.languages.FoldingRangeProvider>();

/** Folding by indentation (what Monaco does by itself for Python), as ranges. */
function indentRanges(model: monaco.editor.ITextModel): monaco.languages.FoldingRange[] {
  const count = model.getLineCount();
  const indents: number[] = [];
  for (let line = 1; line <= count; line++) {
    const text = model.getLineContent(line);
    indents.push(text.trim() ? text.length - text.trimStart().length : -1);
  }
  const ranges: monaco.languages.FoldingRange[] = [];
  const open: { line: number; indent: number }[] = [];
  let lastCode = 0;
  for (let line = 1; line <= count + 1; line++) {
    const indent = line <= count ? indents[line - 1]! : 0;
    if (indent < 0) continue;
    while (open.length && open[open.length - 1]!.indent >= indent) {
      const started = open.pop()!;
      if (lastCode > started.line) ranges.push({ start: started.line, end: lastCode });
    }
    if (line <= count) open.push({ line, indent });
    lastCode = line;
  }
  return ranges;
}

/** Python folds: its blocks, a script's Tables group, each table of it, each cell. */
function foldingRanges(model: monaco.editor.ITextModel): monaco.languages.FoldingRange[] {
  const ranges = indentRanges(model);
  const outline = outlines.get(model);
  if (!outline) return ranges;
  if (outline.region) ranges.push({ start: outline.region[0], end: outline.region[1], kind: monaco.languages.FoldingRangeKind.Region });
  for (const [i, cell] of outline.cells.entries()) {
    // A table's own cells fold as one: its maker's marker down to the end of its transformations.
    const next = outline.cells[i + 1];
    const pair = cell.grouped && cell.role === "maker" && next?.role === "transformations" && next.table === cell.table;
    const end = pair ? next.end : cell.end;
    const alone = cell.role !== "transformations" || outline.cells[i - 1]?.role !== "maker" || outline.cells[i - 1]?.table !== cell.table || !cell.grouped;
    if ((pair || alone) && end > cell.line) ranges.push({ start: cell.line, end });
  }
  return ranges;
}
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
      // A link to a cell: that cell's first line.
      const link = linkAt(model, position);
      if (link) {
        const first = cellLinks.get(model)!.first(link.number);
        const words = first === null ? `There is no cell ${link.number} in this document.` : `Cell ${link.number}` + (first ? "\n```\n" + first.replace(/```/g, "ˋˋˋ") + "\n```" : " (empty)");
        return { range: link.range, contents: [{ value: words, supportHtml: false }] };
      }
      const help = completers.get(model)?.help;
      if (!help) return null;
      const text = await help(model.getValue(), model.getOffsetAt(position));
      return text ? { contents: [{ value: "```\n" + text.replace(/```/g, "ˋˋˋ") + "\n```" }] } : null;
    },
  });
  if (language === "python") monaco.languages.registerFoldingRangeProvider("python", { onDidChange: foldsChanged.event, provideFoldingRanges: (model) => foldingRanges(model) });
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

/** The changed pieces between two lists of lines: `removed` old lines from line `start` (1-based)
 *  become `lines`. The same lines at the start and the end are left alone; a small middle is compared
 *  line by line (longest common run), a large one is one piece. */
export function lineHunks(before: string[], after: string[]): { start: number; removed: number; lines: string[] }[] {
  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head++;
  let tail = 0;
  while (tail < before.length - head && tail < after.length - head && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;
  const a = before.slice(head, before.length - tail);
  const b = after.slice(head, after.length - tail);
  if (!a.length && !b.length) return [];
  if (a.length * b.length > 250_000 || !a.length || !b.length) return [{ start: head + 1, removed: a.length, lines: b }];
  // Longest common subsequence of the middle's lines.
  const width = b.length + 1;
  const table = new Uint32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) table[i * width + j] = a[i] === b[j] ? table[(i + 1) * width + j + 1]! + 1 : Math.max(table[(i + 1) * width + j]!, table[i * width + j + 1]!);
  }
  const hunks: { start: number; removed: number; lines: string[] }[] = [];
  let i = 0;
  let j = 0;
  let open: { start: number; removed: number; lines: string[] } | null = null;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      open = null;
      i++;
      j++;
      continue;
    }
    if (!open) hunks.push((open = { start: head + i + 1, removed: 0, lines: [] }));
    if (j < b.length && (i >= a.length || table[i * width + j + 1]! >= table[(i + 1) * width + j]!)) open.lines.push(b[j++]!);
    else {
      open.removed++;
      i++;
    }
  }
  return hunks;
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
  /** Make the text `text` by changing only the lines that differ, in one edit that undoes with the
   *  typing before it. The cursor stays on its own text and that text stays where it is on screen.
   *  False: nothing differed. */
  replaceText: (text: string) => boolean;
  /** Typing is refused (a table's read-only cell); the app still writes with replaceText. */
  setReadOnly: (readOnly: boolean) => void;
  /** A script's cells: folds for the Tables group, each table and each cell. */
  setOutline: (outline: ScriptOutline | null) => void;
  /** Fold the lines that start at these line numbers (a script's Tables group, a table). */
  fold: (lines: number[]) => void;
  unfold: (lines: number[]) => void;
  /** Words drawn after lines (a table's state beside its marker), and cell markers drawn as rules. */
  setLineNotes: (notes: LineNote[]) => void;
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
  /** Told of every change; `undo`: it was an undo or a redo (the app does not answer those with edits). */
  onChange: (listener: (change: { undo: boolean }) => void) => void;
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
  /** `@cell7` in a comment is a link: a click goes there, a hover shows the cell's first line. */
  cellLinks?: CellLinks;
  readOnly?: boolean;
  /** The right-click menu's Send to the agent: the selected code, or the error under the cursor. */
  sendToAgent?: (what: { text: string; startLine: number; endLine: number; problem: Problem | null }, withQuestion: boolean) => void;
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
    readOnly: options.readOnly ?? false,
    // A read-only cell says nothing when typed in: its head says why.
    readOnlyMessage: { value: "This cell follows the grid: change the table's steps there." },
  });
  const model = editor.getModel();
  if (model && (options.complete || options.help || options.cellLinks)) {
    completers.set(model, { complete: options.complete, help: options.help });
    for (const language of ["python", "sql", "markdown"]) register(language);
  }
  // Cell links: drawn as links, followed on a plain click.
  const linkMarks = editor.createDecorationsCollection([]);
  const drawLinks = (): void => {
    const m = editor.getModel();
    if (!m || !cellLinks.has(m)) return;
    const marks: monaco.editor.IModelDeltaDecoration[] = [];
    for (let line = 1; line <= m.getLineCount(); line++) {
      const text = m.getLineContent(line);
      if (!text.includes("@cell")) continue;
      for (const link of linksOn(text, m.getLanguageId())) marks.push({ range: new monaco.Range(line, link.start, line, link.end), options: { inlineClassName: "wb-cell-link" } });
    }
    linkMarks.set(marks);
  };
  if (model && options.cellLinks) {
    cellLinks.set(model, options.cellLinks);
    drawLinks();
    editor.onDidChangeModelContent(drawLinks);
    editor.onDidChangeModelLanguage(drawLinks);
    editor.onMouseUp((e) => {
      const m = editor.getModel();
      const at = e.target.position;
      if (!m || !at || e.event.leftButton === false || e.event.ctrlKey || e.event.shiftKey || e.event.altKey) return;
      if (e.target.type !== monaco.editor.MouseTargetType.CONTENT_TEXT || !editor.getSelection()?.isEmpty()) return;
      const link = linkAt(m, at);
      if (link) options.cellLinks!.go(link.number);
    });
  }
  const notes = editor.createDecorationsCollection([]);
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

  if (options.sendToAgent) {
    const send = (withQuestion: boolean): void => {
      const model = editor.getModel();
      const chosen = editor.getSelection();
      if (!model || !chosen) return;
      const line = chosen.startLineNumber;
      const problem = chosen.isEmpty() ? (problems.get(model) ?? []).find((p) => p.severity === "error" && line >= p.line && line <= Math.max(p.line, p.endLine)) ?? null : null;
      const range = chosen.isEmpty()
        ? new monaco.Range(problem?.line ?? line, 1, problem ? Math.max(problem.line, problem.endLine) : line, model.getLineMaxColumn(problem ? Math.max(problem.line, problem.endLine) : line))
        : chosen;
      const text = model.getValueInRange(range);
      if (!problem && !text.trim()) return; // a blank line, nothing selected: nothing to send
      options.sendToAgent!({ text, startLine: range.startLineNumber, endLine: range.endLineNumber, problem }, withQuestion);
    };
    editor.addAction({ id: "sag.sendToAgent", label: "Send to the agent", contextMenuGroupId: "0_agent", contextMenuOrder: 1, run: () => send(false) });
    editor.addAction({ id: "sag.sendToAgentAsk", label: "Send to the agent with a question…", contextMenuGroupId: "0_agent", contextMenuOrder: 2, run: () => send(true) });
  }

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
    replaceText: (text) => {
      const m = editor.getModel();
      if (!m || m.getValue() === text) return false;
      const before = m.getLinesContent();
      const after = text.split("\n");
      const hunks = lineHunks(before, after);
      if (!hunks.length) return false;
      // Where each old line lands: a line in a changed piece goes to that piece's new place.
      const mapLine = (line: number): number => {
        let shift = 0;
        for (const hunk of hunks) {
          if (line < hunk.start) break;
          if (line < hunk.start + hunk.removed) return Math.max(1, Math.min(hunk.start + shift + Math.min(line - hunk.start, Math.max(0, hunk.lines.length - 1)), after.length));
          shift += hunk.lines.length - hunk.removed;
        }
        return Math.max(1, Math.min(line + shift, after.length));
      };
      const at = editor.getPosition();
      const topBefore = at ? editor.getTopForLineNumber(at.lineNumber) - editor.getScrollTop() : 0;
      const selections = (editor.getSelections() ?? []).map((sel) => new monaco.Selection(mapLine(sel.selectionStartLineNumber), sel.selectionStartColumn, mapLine(sel.positionLineNumber), sel.positionColumn));
      const last = before.length;
      const edits = hunks.map((hunk) => {
        // Whole lines: from the start of the first changed line (or the end of the line before, at the
        // end of the text) so that no stray newline is left behind.
        if (hunk.start > last) return { range: new monaco.Range(last, m.getLineMaxColumn(last), last, m.getLineMaxColumn(last)), text: "\n" + hunk.lines.join("\n") };
        const end = hunk.start + hunk.removed;
        if (end > last) {
          const from = hunk.start > 1 && !hunk.lines.length ? new monaco.Position(hunk.start - 1, m.getLineMaxColumn(hunk.start - 1)) : new monaco.Position(hunk.start, 1);
          return { range: new monaco.Range(from.lineNumber, from.column, last, m.getLineMaxColumn(last)), text: hunk.lines.join("\n") };
        }
        return { range: new monaco.Range(hunk.start, 1, end, 1), text: hunk.lines.length ? hunk.lines.join("\n") + "\n" : "" };
      });
      const wasReadOnly = editor.getOption(monaco.editor.EditorOption.readOnly);
      if (wasReadOnly) editor.updateOptions({ readOnly: false });
      editor.executeEdits("sag-tables", edits, selections.length ? selections : undefined);
      if (wasReadOnly) editor.updateOptions({ readOnly: true });
      // The line the cursor is on stays where it was on screen (a script scrolls by the lines added above).
      const now = editor.getPosition();
      if (at && now && !options.grow) editor.setScrollTop(editor.getTopForLineNumber(now.lineNumber) - topBefore);
      return true;
    },
    setReadOnly: (readOnly) => {
      if (editor.getOption(monaco.editor.EditorOption.readOnly) !== readOnly) editor.updateOptions({ readOnly });
    },
    setOutline: (outline) => {
      const m = editor.getModel();
      if (!m) return;
      if (outline) outlines.set(m, outline);
      else outlines.delete(m);
      foldsChanged.fire({ provideFoldingRanges: (model) => foldingRanges(model) });
    },
    fold: (lines) => {
      if (lines.length) void editor.getAction("editor.fold")?.run({ selectionLines: lines.map((l) => l - 1), levels: 1 });
    },
    unfold: (lines) => {
      if (lines.length) void editor.getAction("editor.unfold")?.run({ selectionLines: lines.map((l) => l - 1), levels: 1 });
    },
    setLineNotes: (list) => {
      const m = editor.getModel();
      if (!m) return;
      notes.set(
        list
          .filter((n) => n.line >= 1 && n.line <= m.getLineCount())
          .flatMap((n) => {
            const end = m.getLineMaxColumn(n.line);
            const marks: monaco.editor.IModelDeltaDecoration[] = [];
            if (n.marker) marks.push({ range: new monaco.Range(n.line, 1, n.line, 1), options: { isWholeLine: true, className: n.tone === "stale" ? "wb-marker-line stale" : "wb-marker-line" } });
            if (n.text) marks.push({ range: new monaco.Range(n.line, Math.max(1, end - 1), n.line, end), options: { showIfCollapsed: true, after: { content: `   ${n.text}`, inlineClassName: n.tone === "stale" ? "wb-line-note stale" : "wb-line-note" } } });
            return marks;
          }),
      );
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
    onChange: (listener) => void editor.onDidChangeModelContent((e) => listener({ undo: e.isUndoing || e.isRedoing })),
    layout: () => (options.grow ? fit() : editor.layout()),
    dispose: () => {
      editor.getModel()?.dispose();
      editor.dispose();
    },
  };
}
