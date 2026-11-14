/**
 * workbenchScript.ts: a Python script in the Workbench: the editor above, its Python console below
 * (like Spyder or VS Code's interactive window), both in the document's IPython kernel. [WIRE]
 *
 *   [RUN]      Run file (F5) runs the whole script as it stands in the editor (saved or not); Run
 *              selection (Ctrl+Enter, F9) the selected lines, or the cursor's line
 *   [CONSOLE]  each run's code and outputs; a line typed below runs there too (Up and Down go back
 *              through what was typed); Clear empties it
 */
import { button, clear, h } from "./dom";
import type { CodeEditor } from "./lazy/codeEditor";
import { pythonCompleter } from "./workbenchCompletion";
import { drawOutputs, type Kernel, type Output } from "./workbenchKernel";

export interface ScriptView {
  element: HTMLElement;
  text: () => string;
  runFile: () => Promise<void>;
  runSelection: () => Promise<void>;
  setTheme: (theme: string) => void;
}

/** Python's own dedent for a selection run from inside a block: the lines lose their shared indent. */
function dedent(code: string): string {
  const lines = code.split("\n");
  const indents = lines.filter((l) => l.trim()).map((l) => /^[ \t]*/.exec(l)![0].length);
  const cut = indents.length ? Math.min(...indents) : 0;
  return lines.map((l) => l.slice(cut)).join("\n");
}

export function scriptView(
  initial: string,
  kernel: Kernel,
  theme: () => string,
  changed: () => void,
  names: () => { connections: string[]; blades: string[] } = () => ({ connections: [], blades: [] }),
): ScriptView {
  const editorHost = h("div", { class: "wb-script-editor" });
  const log = h("div", { class: "wb-console-log", role: "log", "aria-live": "polite" });
  const input = h("input", { type: "text", class: "mono wb-console-input", "aria-label": "Python console: type a line and press Enter", placeholder: ">>> a line of Python, Enter runs it", spellcheck: false });
  const history: string[] = [];
  let back = 0;
  let editor: CodeEditor | null = null;
  let text = initial;
  const dark = (): boolean => theme().endsWith("dark");

  const run = async (code: string, label: string): Promise<void> => {
    if (!code.trim()) return;
    const outputs: Output[] = [];
    const outputHost = h("div", { class: "wb-outputs" });
    log.append(h("div", { class: "wb-console-entry" }, h("pre", { class: "wb-console-code" }, label), outputHost));
    log.scrollTop = log.scrollHeight;
    try {
      await kernel.run({ code }, (o) => {
        if ((o as { clear?: boolean }).clear) outputs.length = 0;
        else outputs.push(o);
        drawOutputs(outputHost, outputs, dark());
        log.scrollTop = log.scrollHeight;
      });
    } catch (e) {
      drawOutputs(outputHost, [{ output_type: "error", traceback: [(e as Error).message] }], dark());
    }
    log.scrollTop = log.scrollHeight;
  };

  const runFile = async (): Promise<void> => {
    const code = editor ? editor.value() : text;
    const lines = code.split("\n").length;
    await run(code, `# the script (${lines} line${lines === 1 ? "" : "s"})`);
  };
  const runSelection = async (): Promise<void> => {
    if (!editor) return;
    const chosen = dedent(editor.selection());
    await run(chosen, chosen.trimEnd());
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && input.value.trim()) {
      const line = input.value;
      history.push(line);
      back = history.length;
      input.value = "";
      void run(line, `>>> ${line}`);
    } else if (e.key === "ArrowUp" && back > 0) {
      input.value = history[--back] ?? "";
      e.preventDefault();
    } else if (e.key === "ArrowDown") {
      back = Math.min(history.length, back + 1);
      input.value = history[back] ?? "";
      e.preventDefault();
    }
  });

  void (import(/* external */ "/static/build/lazy/codeEditor.js" as string) as Promise<typeof import("./lazy/codeEditor")>).then((m) => {
    editor = m.createCodeEditor(editorHost, {
      language: "python",
      value: initial,
      theme: theme(),
      keys: { f5: () => void runFile(), ctrlEnter: () => void runSelection() },
      complete: pythonCompleter(kernel, names),
      help: (text, offset) => kernel.help(text, offset),
    });
    editor.onChange(() => {
      text = editor!.value();
      changed();
    });
  });

  const element = h(
    "div",
    { class: "wb-script" },
    editorHost,
    h(
      "div",
      { class: "wb-console" },
      h(
        "div",
        { class: "wb-console-head" },
        h("strong", {}, "Python console"),
        h("span", { class: "hint" }, "F5 runs the script; Ctrl+Enter the selection or the line"),
        h("span", { class: "spacer" }),
        button("Clear", () => clear(log), { icon: "trash", class: "ghost" }),
      ),
      log,
      input,
    ),
  );
  return {
    element,
    text: () => (editor ? editor.value() : text),
    runFile,
    runSelection,
    setTheme: (t) => editor?.setTheme(t),
  };
}
