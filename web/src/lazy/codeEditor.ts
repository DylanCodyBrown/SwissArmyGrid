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
    fixedOverflowWidgets: true,
    placeholder: options.placeholder,
  });
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
