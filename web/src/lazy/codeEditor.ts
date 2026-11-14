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
export type Helper = (text: string, offset: number) => Promise<string>;

// Each editor's own completer and help (a notebook cell's kernel and connection, a script's kernel).
const completers = new WeakMap<monaco.editor.ITextModel, { complete?: Completer; help?: Helper }>();
const registered = new Set<string>();
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
      const help = completers.get(model)?.help;
      if (!help) return null;
      const text = await help(model.getValue(), model.getOffsetAt(position));
      return text ? { contents: [{ value: "```\n" + text.replace(/```/g, "ˋˋˋ") + "\n```" }] } : null;
    },
  });
}

export interface CodeEditor {
  value: () => string;
  setValue: (text: string) => void;
  /** The selected text, or the cursor's line when nothing is selected. */
  selection: () => string;
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
  /** Keys: Shift+Enter, Ctrl+Enter (and F9), F5. */
  keys?: { shiftEnter?: () => void; ctrlEnter?: () => void; f5?: () => void; escape?: () => void };
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
  if (keys.shiftEnter) editor.addCommand(monaco.KeyMod.Shift | monaco.KeyCode.Enter, keys.shiftEnter);
  if (keys.ctrlEnter) {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, keys.ctrlEnter);
    editor.addCommand(monaco.KeyCode.F9, keys.ctrlEnter);
  }
  if (keys.f5) editor.addCommand(monaco.KeyCode.F5, keys.f5);
  if (keys.escape) editor.addCommand(monaco.KeyCode.Escape, keys.escape, "!suggestWidgetVisible");

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
