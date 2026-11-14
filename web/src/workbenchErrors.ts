/**
 * workbenchErrors.ts: the error panel, Visual Studio's Exception Helper for the Workbench. [FUNCTION]
 *
 * After a run fails the server sends `error_detail` (client/kernel_errors.py gathers it in the kernel,
 * domain/errors.py writes the words). Drawn here, in place of the plain red traceback:
 *
 *   HEADER     the error's type and message, Python's own "Did you mean", where in your code it happened
 *   PLAIN      what it means, in a sentence or two
 *   FIXES      one click each: use the close name, add the import, install the package, run the cell
 *              that makes the name
 *   VALUES     the failing expression's parts at that moment (`rates[region]`, `region` = 'south'), then
 *              the function's own variables
 *   CALL STACK your frames first-class, each run of library frames folded ("12 frames in pandas");
 *              a frame in a cell or the script goes to its line
 *   CHAIN      the error that led to this one ("caused by", "while handling")
 *   TRACEBACK  Python's own text, in its colours, behind a toggle; Copy takes it without colours
 *   AGENT      Send to the agent (the button, or right-click the panel): the error, its place, its
 *              values and the traceback's end go to the agent's inbox (workbenchAgent.ts)
 *
 * The editor side (the squiggle, the end-of-line message, the hover and Ctrl+. fixes) is placed by the
 * notebook and the script from `problemsFor`.
 */
import { button, h, toast } from "./dom";
import { contextMenu, currentDocument, errorText, sendToAgent } from "./workbenchAgent";
import type { Problem } from "./lazy/codeEditor";

export interface Fix {
  kind: "replace" | "import" | "install" | "define";
  title: string;
  target?: string;
  text?: string;
  line?: string;
  package?: string;
  name?: string;
}

export interface Frame {
  where: "cell" | "file" | "library" | "stdlib";
  cell: number | null;
  file: string | null;
  module: string;
  function: string;
  line: number;
  end_line: number | null;
  col: number | null;
  end_col: number | null;
  code: string;
  values: { expr: string; repr: string; type: string }[];
  locals: { name: string; repr: string; type: string; table: boolean }[];
}

export interface Entry {
  type: string;
  message: string;
  hint: string;
  relation: "cause" | "context" | null;
  notes: string[];
  syntax: { where: string; cell: number | null; file: string | null; line: number; end_line: number; col: number; end_col: number | null; text: string } | null;
  frames: Frame[];
  your_frame: number | null;
  group: string[];
  title: string;
  explanation: string;
  fixes: Fix[];
  sql: { line?: number; col?: number; missing?: string; candidates?: string[] } | null;
}

export interface ErrorDetail {
  chain: Entry[];
  where?: string;
}

export interface ErrorActions {
  /** Apply a fix (the notebook or script knows where the code is). */
  fix: (fix: Fix, detail: ErrorDetail) => void;
  /** Go to a frame's line in a cell or the script (false: not one of this document's). */
  goTo: (frame: Frame) => boolean;
  /** Open a frame's table variable as a grid. */
  openGrid?: (name: string) => void;
}

const last = (detail: ErrorDetail): Entry | undefined => detail.chain[detail.chain.length - 1];

/** The frame in the user's own code the error went through last. */
export function yourFrame(entry: Entry | undefined): Frame | null {
  if (!entry) return null;
  const at = entry.your_frame;
  return at !== null && at !== undefined ? (entry.frames[at] ?? null) : null;
}

function frameLabel(f: Frame): string {
  const place = f.where === "cell" ? `cell [${f.cell ?? "?"}]` : (f.file ?? "").split(/[\\/]/).pop() || f.module;
  const fn = f.function && f.function !== "<module>" ? `${f.function}(), ` : "";
  return `${fn}${place}, line ${f.line}`;
}

function libraryOf(f: Frame): string {
  return (f.module || (f.file ?? "").split(/[\\/]/).slice(-2, -1)[0] || "a library").split(".")[0]!;
}

function valuesTable(rows: { name: string; repr: string; type: string; table?: boolean }[], actions: ErrorActions): HTMLElement {
  return h(
    "table",
    { class: "wb-err-values" },
    h("tbody", {}, ...rows.map((r) => h("tr", {}, h("th", { scope: "row" }, h("code", {}, r.name)), h("td", { class: "mono" }, r.repr, r.table && actions.openGrid ? button(`Open ${r.name} as a grid`, () => actions.openGrid!(r.name), { icon: "export", iconOnly: true, class: "ghost" }) : null), h("td", { class: "hint" }, r.type)))),
  );
}

/** The panel for one failed run. `traceback` is Python's own text, already drawn (kept behind a toggle). */
export function errorPanel(detail: ErrorDetail, traceback: HTMLElement | null, actions: ErrorActions): HTMLElement {
  const entry = last(detail)!;
  const frame = yourFrame(entry);
  const head = h(
    "div",
    { class: "wb-err-head" },
    h("span", { class: "wb-err-type" }, entry.type),
    h("span", { class: "wb-err-message" }, entry.message ? `: ${entry.message}` : ""),
  );
  const send = (withQuestion: boolean): void =>
    sendToAgent({ kind: "error", title: `An error in ${currentDocument()}${detail.where ? ` (${detail.where})` : ""}`, text: errorText(detail, traceback?.textContent ?? "") }, withQuestion);
  head.append(button("Send to the agent", () => send(false), { icon: "agent", class: "ghost wb-err-send", title: "Send this error to your agent (right-click for more)" }));
  const parts: (HTMLElement | null)[] = [head];
  if (entry.hint) parts.push(h("div", { class: "wb-err-hint" }, entry.hint));
  if (detail.where) {
    const where = h("button", { type: "button", class: "link wb-err-where" }, `In your code: ${detail.where}`);
    const target = entry.syntax ? ({ where: entry.syntax.where, cell: entry.syntax.cell, file: entry.syntax.file, line: entry.syntax.line, col: entry.syntax.col } as unknown as Frame) : frame;
    where.addEventListener("click", () => target && actions.goTo(target));
    parts.push(where);
  }
  if (entry.explanation) parts.push(h("p", { class: "wb-err-explain" }, entry.explanation));
  for (const note of entry.notes) parts.push(h("p", { class: "hint" }, note));
  if (entry.group.length) parts.push(h("ul", { class: "wb-err-group" }, ...entry.group.map((g) => h("li", { class: "mono" }, g))));
  if (entry.fixes.length) {
    parts.push(h("div", { class: "wb-err-fixes", role: "group", "aria-label": "Fixes" }, ...entry.fixes.map((f) => button(f.title.replace(/`/g, ""), () => actions.fix(f, detail), { icon: f.kind === "install" ? "import" : f.kind === "define" ? "play" : "edit", class: "ghost" }))));
  }
  if (frame && (frame.values.length || frame.locals.length)) {
    parts.push(
      h(
        "details",
        { class: "wb-err-section", open: true },
        h("summary", {}, "At that moment"),
        frame.values.length ? valuesTable(frame.values.map((v) => ({ name: v.expr, repr: v.repr, type: v.type })), actions) : null,
        frame.locals.length ? h("div", {}, h("div", { class: "hint" }, `${frame.function}()'s variables`), valuesTable(frame.locals, actions)) : null,
      ),
    );
  }
  if (entry.frames.length > 1) parts.push(callStack(entry.frames, frame, actions));
  const earlier = detail.chain.slice(0, -1);
  if (earlier.length) {
    parts.push(
      h(
        "details",
        { class: "wb-err-section" },
        h("summary", {}, earlier.length === 1 ? "The error that led to it" : `The ${earlier.length} errors that led to it`),
        ...earlier.map((e) => h("div", { class: "wb-err-chained" }, h("strong", {}, `${e.type}: `), e.message, h("div", { class: "hint" }, e.relation === "cause" ? "…was the direct cause of the next one" : "…and while handling it, the next one happened"), e.explanation ? h("p", {}, e.explanation) : null)),
      ),
    );
  }
  if (traceback) {
    const copy = button("Copy the traceback", () => void navigator.clipboard?.writeText(traceback.textContent ?? "").catch(() => undefined), { icon: "copy", iconOnly: true, class: "ghost" });
    parts.push(h("details", { class: "wb-err-section" }, h("summary", {}, "Python's traceback ", copy), traceback));
  }
  const panel = h("div", { class: "wb-err", role: "group", "aria-label": `${entry.type}: the error and what to do` }, ...parts);
  panel.addEventListener("contextmenu", (e) =>
    contextMenu(e, [
      { label: "Send this error to the agent", run: () => send(false) },
      { label: "Send it with a question…", run: () => send(true) },
      { label: "Copy the error", run: () => void navigator.clipboard?.writeText(errorText(detail, traceback?.textContent ?? "")).then(() => toast("Copied."), () => undefined) },
    ]),
  );
  return panel;
}

/** Your frames one by one; each run of library frames folded into one row that opens. */
function callStack(frames: Frame[], current: Frame | null, actions: ErrorActions): HTMLElement {
  const list = h("ol", { class: "wb-err-stack" });
  let i = 0;
  while (i < frames.length) {
    const f = frames[i]!;
    if (f.where === "library" || f.where === "stdlib") {
      const run: Frame[] = [];
      while (i < frames.length && (frames[i]!.where === "library" || frames[i]!.where === "stdlib")) run.push(frames[i++]!);
      const names = [...new Set(run.map(libraryOf))].join(", ");
      const folded = h("details", { class: "wb-err-lib" }, h("summary", {}, `${run.length} frame${run.length === 1 ? "" : "s"} in ${names}`), h("ol", {}, ...run.map((r) => h("li", { class: "mono hint" }, `${r.module || r.file}: ${r.function}, line ${r.line}`, r.code ? h("div", {}, r.code.trim()) : null))));
      list.append(h("li", {}, folded));
      continue;
    }
    const go = h("button", { type: "button", class: `link wb-err-frame${f === current ? " current" : ""}` }, frameLabel(f));
    go.addEventListener("click", () => actions.goTo(f));
    list.append(h("li", {}, go, f.code ? h("div", { class: "mono wb-err-code" }, f.code.trim()) : null));
    i++;
  }
  return h("details", { class: "wb-err-section", open: frames.length <= 6 }, h("summary", {}, "Call stack (most recent last)"), list);
}

/** Squiggles for an editor: the failing place (an error) and the call sites that led to it (notes).
 *  `place` maps a frame to this editor's lines, or null when the frame is not in it. */
export function problemsFor(detail: ErrorDetail, place: (frame: Frame) => { line: number; colShift: number } | null): Problem[] {
  const entry = last(detail);
  if (!entry) return [];
  const problems: Problem[] = [];
  const message = `${entry.type}${entry.message ? `: ${entry.message}` : ""}`;
  const hover = [
    `**${entry.type}**${entry.message ? `: ${entry.message}` : ""}`,
    entry.hint ? `_${entry.hint}_` : "",
    entry.explanation,
    ...(yourFrame(entry)?.values.length ? ["At that moment: " + yourFrame(entry)!.values.slice(0, 6).map((v) => `\`${v.expr}\` = \`${v.repr.replace(/`/g, "ˋ")}\``).join(", ")] : []),
  ]
    .filter(Boolean)
    .join("\n\n");
  const fixes = entry.fixes.filter((f) => f.kind === "replace" || f.kind === "import");
  if (entry.syntax) {
    const at = place({ ...entry.syntax, where: entry.syntax.where as Frame["where"], function: "", module: "", end_line: entry.syntax.end_line, end_col: entry.syntax.end_col, code: entry.syntax.text, values: [], locals: [] });
    if (at) problems.push({ line: at.line, col: entry.syntax.col + at.colShift, endLine: at.line, endCol: entry.syntax.end_col ? entry.syntax.end_col + at.colShift : null, message, hover, severity: "error", fixes });
    return problems;
  }
  const yours = yourFrame(entry);
  for (const f of entry.frames) {
    if (f.where !== "cell" && f.where !== "file") continue;
    const at = place(f);
    if (!at) continue;
    const isError = f === yours;
    const endLine = at.line + ((f.end_line ?? f.line) - f.line);
    problems.push({
      line: at.line,
      col: f.col ? f.col + at.colShift : null,
      endLine,
      endCol: f.end_col ? f.end_col + at.colShift : null,
      message: isError ? message : `Called here, on the way to ${message}`,
      hover: isError ? hover : `Called here: the error happened further in (${frameLabel(yours ?? f)}).`,
      severity: isError ? "error" : "info",
      fixes: isError ? fixes : [],
    });
  }
  return problems;
}

/** A SQL cell's error: the place the database pointed at, in the cell's own lines. */
export function sqlProblems(detail: ErrorDetail): Problem[] {
  const entry = last(detail);
  const sql = entry?.sql;
  if (!entry || !sql?.line) return [];
  const width = sql.missing ? sql.missing.length : 1;
  return [
    {
      line: sql.line,
      col: sql.col ?? 1,
      endLine: sql.line,
      endCol: (sql.col ?? 1) + width,
      message: entry.message.split("\n")[0] ?? entry.type,
      hover: [`**The SQL did not run**`, entry.message.split("\n").slice(0, 2).join("  \n"), sql.candidates?.length ? `Close names: ${sql.candidates.map((c) => `\`${c}\``).join(", ")}` : ""].filter(Boolean).join("\n\n"),
      severity: "error",
      fixes: entry.fixes.filter((f) => f.kind === "replace"),
    },
  ];
}
