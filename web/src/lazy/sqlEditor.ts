/**
 * lazy/sqlEditor.ts: the Monaco SQL editor (loaded on first use of the SQL box). [WIRE]
 *
 * Column names (and `grid` plus open dataset names) autocomplete; Ctrl+Enter runs. The theme
 * follows the page: Monaco's built-in vs, vs-dark, hc-light and hc-black match our four themes.
 */
import * as monaco from "monaco-editor";

(self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
  getWorker: () => new Worker("/static/build/editor.worker.js", { type: "module" }),
};

const THEMES: Record<string, string> = { light: "vs", dark: "vs-dark", "hc-light": "hc-light", "hc-dark": "hc-black" };
// Each editor's own names: the SQL box offers one grid's columns, a query tray every open table's.
const namesOf = new WeakMap<monaco.editor.ITextModel, { columns: string[]; tables: string[] }>();
let registered = false;

export interface SqlEditor {
  value: () => string;
  setValue: (text: string) => void;
  focus: () => void;
  setTheme: (theme: string) => void;
  setNames: (columns: string[], tables: string[]) => void;
  /** Free the editor and its text model (a "+" tab's tray when its tab closes). */
  dispose: () => void;
}

export function createSqlEditor(host: HTMLElement, initial: string, onRun: () => void, theme: string): SqlEditor {
  if (!registered) {
    registered = true;
    monaco.languages.registerCompletionItemProvider("sql", {
      provideCompletionItems: (model, position) => {
        const word = model.getWordUntilPosition(position);
        const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
        const quote = (n: string): string => (/^[a-z_][a-z0-9_]*$/.test(n) ? n : `"${n.replace(/"/g, '""')}"`);
        const names = namesOf.get(model) ?? { columns: [], tables: [] };
        return {
          suggestions: [
            ...names.columns.map((n) => ({ label: n, kind: monaco.languages.CompletionItemKind.Field, insertText: quote(n), range, detail: "column" })),
            ...names.tables.map((n) => ({ label: n, kind: monaco.languages.CompletionItemKind.Struct, insertText: quote(n), range, detail: n === "grid" ? "this grid's view" : "dataset" })),
          ],
        };
      },
    });
  }
  const editor = monaco.editor.create(host, {
    value: initial,
    language: "sql",
    theme: THEMES[theme] ?? "vs",
    minimap: { enabled: false },
    automaticLayout: true,
    fontSize: 13,
    scrollBeyondLastLine: false,
    wordWrap: "on",
    ariaLabel: "SQL query",
  });
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, onRun);
  return {
    value: () => editor.getValue(),
    setValue: (text) => editor.setValue(text),
    focus: () => editor.focus(),
    setTheme: (t) => monaco.editor.setTheme(THEMES[t] ?? "vs"),
    setNames: (columns, tables) => {
      const model = editor.getModel();
      if (model) namesOf.set(model, { columns, tables });
    },
    // The editor made its model from `value`, so disposing the editor disposes the model too.
    dispose: () => editor.dispose(),
  };
}
