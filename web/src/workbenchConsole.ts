/**
 * workbenchConsole.ts: a document's Python console, in the Workbench's console pane (RStudio's
 * bottom-left). Every notebook and script has one, on its own kernel: a notebook gets a scratch prompt
 * beside its cells, a script's runs show here. [WIRE]
 *
 *   [LOG]     each run's code and outputs, the newest followed unless scrolled up to read
 *   [PROMPT]  several lines: Enter runs a whole statement and adds a line to an unfinished one (as
 *             IPython does), Shift+Enter always adds a line, Ctrl+Enter runs as it stands; Up and Down
 *             go back through what ran (the half-typed text comes back); Tab completes, hover helps
 *   [CLEAR]   empties the log (a run still going keeps its place; the variables stay)
 */
import { button, h } from "./dom";
import type { CodeEditor, Completer, Helper } from "./lazy/codeEditor";
import { codeItem, currentDocument, sendToAgent } from "./workbenchAgent";
import { type Kernel, runInto, type RunBody } from "./workbenchKernel";
import { type ErrorHooks, outputArea, resultName } from "./workbenchOutputs";

/** Where a run's code came from, so an error's lines map back to the script: the file (Run file), or
 *  lines from `start` that lost `indent` columns to the dedent (a statement or a selection). */
export type RunOrigin = { kind: "file"; path: string | null } | { kind: "lines"; start: number; indent: number };

export interface ConsoleView {
  element: HTMLElement;
  /** Run in the document's kernel, shown in the log under `label` (the code, or "%run file.py").
   *  True: the run ended without an error. */
  run: (body: RunBody, label: string, origin?: RunOrigin) => Promise<boolean>;
  /** A line of the page's own (the script is empty, say). */
  note: (message: string) => void;
  clear: () => void;
  focus: () => void;
  /** What the log shows, as text (for the agent's workbench_window "console.read"). */
  text: () => string;
  setTheme: (theme: string) => void;
}

let editors: Promise<typeof import("./lazy/codeEditor")> | null = null;
export const loadEditors = (): Promise<typeof import("./lazy/codeEditor")> =>
  (editors ??= import(/* external */ "/static/build/lazy/codeEditor.js" as string) as Promise<typeof import("./lazy/codeEditor")>);

export function consoleView(
  kernel: Kernel,
  theme: () => string,
  options: { complete: Completer; help: Helper; f5?: () => void; errors?: (origin?: RunOrigin) => ErrorHooks; ran?: () => void },
): ConsoleView {
  const log = h("div", { class: "wb-console-log", role: "log", "aria-label": "Console output" });
  const promptHost = h("div", { class: "wb-console-editor" });
  const history: string[] = [];
  let back = 0;
  let draft = "";
  let prompt: CodeEditor | null = null;
  let stick = true;
  const running = new Set<Element>();
  const dark = (): boolean => theme().endsWith("dark");

  log.addEventListener("scroll", () => {
    stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  });
  const follow = (): void => {
    if (stick) log.scrollTop = log.scrollHeight;
  };

  const run = async (body: RunBody, label: string, origin?: RunOrigin): Promise<boolean> => {
    options.ran?.(); // a run shows the console at its size
    // A table result is named after the code's last variable (`orders` -> the grid "orders").
    const hooks = options.errors?.(origin);
    const open = hooks?.openResult;
    const area = outputArea(dark, follow, hooks && { ...hooks, openResult: open && ((n, name) => open(n, name ?? resultName(body.kind === "sql" ? "" : body.code))) });
    const entry = h("div", { class: "wb-console-entry running" }, h("pre", { class: "wb-console-code" }, label), area.element);
    log.append(entry);
    running.add(entry);
    stick = true;
    follow();
    try {
      return (await runInto(kernel, body, area)).ok;
    } finally {
      running.delete(entry);
      entry.classList.remove("running");
      follow();
    }
  };

  const note = (message: string): void => {
    log.append(h("div", { class: "wb-console-entry" }, h("div", { class: "wb-notice" }, message)));
    stick = true;
    follow();
  };

  const submit = (): void => {
    if (!prompt) return;
    const code = prompt.value();
    if (!code.trim()) return;
    if (history[history.length - 1] !== code) history.push(code);
    back = history.length;
    draft = "";
    prompt.setValue("");
    void run({ code }, code.split("\n").map((l, i) => `${i ? "..." : ">>>"} ${l}`).join("\n"));
  };

  const enter = async (): Promise<void> => {
    if (!prompt) return;
    const code = prompt.value();
    if (!code.trim()) return;
    // In the middle of several lines Enter is a new line; at the end, a whole statement runs.
    if (code.includes("\n") && !prompt.atEnd()) return prompt.insertNewline();
    const verdict = await kernel.isComplete(code);
    if (verdict.status === "incomplete") prompt.insertNewline();
    else submit();
  };

  const browse = (by: -1 | 1): boolean => {
    if (!prompt) return false;
    const onEdge = by < 0 ? prompt.cursorLine() === 1 : prompt.cursorLine() === prompt.lineCount();
    if (!onEdge) return false;
    if (by < 0 && back === 0) return true;
    if (by > 0 && back >= history.length) return true;
    if (back === history.length) draft = prompt.value(); // keep what was half typed
    back += by;
    prompt.setValue(back === history.length ? draft : (history[back] ?? ""));
    prompt.moveTo(by < 0 ? 1 : prompt.lineCount());
    return true;
  };

  void loadEditors().then((m) => {
    prompt = m.createCodeEditor(promptHost, {
      language: "python",
      value: "",
      theme: theme(),
      grow: true,
      placeholder: "Python",
      keys: {
        enter: () => void enter(),
        shiftEnter: () => prompt?.insertNewline(),
        ctrlEnter: submit,
        f5: options.f5,
        up: () => browse(-1),
        down: () => browse(1),
      },
      complete: options.complete,
      help: options.help,
      sendToAgent: (what, ask) => sendToAgent(codeItem(`the console of ${currentDocument()}`, what, "python"), ask),
    });
  });

  const clear = (): void => {
    // A run still going keeps its entry (its output would otherwise vanish as it arrives).
    for (const entry of Array.from(log.children)) if (!running.has(entry)) entry.remove();
  };

  const element = h(
    "section",
    { class: "wb-console", "aria-label": "Python console" },
    h(
      "div",
      { class: "wb-console-head" },
      h("strong", {}, "Console"),
      h("span", { class: "spacer" }),
      button("Clear", clear, { icon: "trash", class: "ghost" }),
    ),
    log,
    h("div", { class: "wb-console-prompt" }, h("span", { class: "wb-console-chevron mono", "aria-hidden": "true" }, ">>>"), promptHost),
  );
  // A click on the log's empty space puts the cursor in the prompt (as a terminal does).
  log.addEventListener("click", (e) => {
    if (e.target === log && !getSelection()?.toString()) prompt?.focus();
  });
  return {
    element,
    run,
    note,
    clear,
    focus: () => prompt?.focus(),
    text: () => log.innerText,
    setTheme: (t) => prompt?.setTheme(t),
  };
}
