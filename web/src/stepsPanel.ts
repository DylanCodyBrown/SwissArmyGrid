/**
 * stepsPanel.ts: the Table Transformations panel (applied steps) and its Code tab
 * (plan sections 7, 9, 10). [WIRE]
 * It sits on the right of the grid and collapses to a slim labelled rail; the choice is remembered.
 *
 * ROUTING: [COLLAPSE TOGGLE]                 -> setCollapsed() (workspace data-steps="open" | "collapsed")
 *          [ADD SQL STEP]    -> editStep("sql") (a SQL step's own form)
 *          [COPY AS SQL OR PYTHON]           -> showCode() (the Code tab: the verified query + Copy as SQL / Python)
 *          [STEP ROW]        click text -> state.setPreview(i + 1); click again -> full view
 *          [ON/OFF BOX]      -> toggle(i)        [DRAG GRIP] -> drop(from, to)
 *          [EDIT / DELETE]   -> editStep(kind, i) / remove(i)
 *          [ROW COUNTS]      -> read-only, from GET /grids/{id}/step-counts
 *          [STEP ERROR]      -> read-only, from state.info.step_error
 *          [CATEGORIZE INFO] -> read-only, from GET /grids/{id}/categorize-stats
 *          [ADD STEP MENU]   -> editStep(kind)
 *          [CODE TAB]        -> renderCode(target); the Copy buttons only when the code verified identical
 *          [POP OUT]         -> window.open(?panel=steps); both windows follow the server
 */
import { api, clientId, grid, gridId, query, token, type StepJson } from "./api";
import { button, clear, formatCount, h, icon, recall, remember, toast } from "./dom";
import { KIND_LABELS, editStep } from "./stepForms";
import { state } from "./state";

let inPopout = false;
let tab: "steps" | "code" = "steps";
let codeTarget: string | null = null;
let counts: number[] = [];
let dragFrom: number | null = null;
let host: HTMLElement;
// The workspace carries data-steps, which the stylesheet turns into the panel's width. Null in the pop-out.
let workspace: HTMLElement | null = null;

export function mountStepsPanel(element: HTMLElement, popout: boolean): void {
  host = element;
  inPopout = popout;
  // Switching tabs announces both: one render (and one step-counts request), not two.
  state.on("info", scheduleRender);
  state.on("preview", scheduleRender);
  if (!popout) {
    workspace = element.closest<HTMLElement>(".workspace");
    if (workspace) workspace.dataset.steps = recall("steps.mode") === "collapsed" ? "collapsed" : "open";
    return;
  }
  document.body.classList.add("popout");
}

let renderQueued = false;

function scheduleRender(): void {
  if (renderQueued) return;
  renderQueued = true;
  queueMicrotask(() => {
    renderQueued = false;
    void render();
  });
}

function isCollapsed(): boolean {
  return workspace?.dataset.steps === "collapsed";
}

function setCollapsed(collapsed: boolean): void {
  if (!workspace) return;
  workspace.dataset.steps = collapsed ? "collapsed" : "open";
  remember("steps.mode", collapsed ? "collapsed" : "open");
  void render();
}

export function showCode(): void {
  tab = "code";
  void render();
}

async function render(): Promise<void> {
  const info = state.info;
  if (!info || !host) return;
  const collapsed = isCollapsed();
  const header = h(
    "div",
    { class: "panel-header" },
    h("h2", { id: "steps-title" }, "Table Transformations"),
    button("Pop out the transformations panel", popOut, { icon: "popout", iconOnly: true, class: "ghost" }),
    // Last, at the right edge, where the panel collapses to. The pop-out window has no grid beside it to make room for.
    inPopout
      ? null
      : h(
          "span",
          { class: "collapse-toggle" },
          button(collapsed ? "Open the transformations panel" : "Collapse the transformations panel", () => setCollapsed(!collapsed), {
            icon: collapsed ? "collapse" : "expand",
            iconOnly: true,
            class: "ghost",
          }),
          collapsed ? h("span", { class: "rail-label" }, "Table Transformations") : null,
        ),
  );
  if (collapsed) {
    clear(host, header);
    return;
  }
  const body = h("div", { class: "panel-body" });
  clear(
    host,
    header,
    h(
      "div",
      { class: "panel-actions" },
      button("Add a SQL step", () => editStep("sql"), {
        icon: "sql",
        title: "One SELECT over the table entering the step (as grid, or by its name)",
      }),
      button("Copy transformations as SQL or Python", () => showCode(), {
        icon: "code",
        pressed: tab === "code",
        title: "These transformations as one DuckDB query over your own table, verified against the grid",
      }),
    ),
    h(
      "div",
      { class: "tabs", role: "tablist", "aria-labelledby": "steps-title" },
      h("button", { class: "tab", role: "tab", "aria-selected": String(tab === "steps"), onclick: () => { tab = "steps"; void render(); } }, "Steps"),
      h("button", { class: "tab", role: "tab", "aria-selected": String(tab === "code"), onclick: () => { tab = "code"; void render(); } }, "Code"),
    ),
    body,
  );
  if (tab === "code") {
    await renderCode(body);
    return;
  }
  try {
    counts = (await api<{ counts: number[] }>(`${grid}/step-counts${query({ gen: state.gen, client: clientId })}`)).counts;
  } catch {
    counts = [];
  }
  const list = h("ol", { class: "step-list", "aria-label": "Steps, in order" });
  list.append(
    h(
      "li",
      { class: "step source" },
      h("span", { class: "num", "aria-hidden": "true" }, "0"),
      h("span", { class: "text", onclick: () => state.setPreview(state.previewUpto === 0 ? null : 0) }, `Source: ${info.dataset} (${formatCount(info.row_count)} rows, version ${info.version})`),
    ),
  );
  info.steps.forEach((step, index) => {
    list.append(stepRow(step, index));
  });
  body.append(list);
  if (info.steps.length) body.append(countLine(info.steps.length, true));
  body.append(
    ...(info.steps.length ? [] : [h("p", { class: "hint" }, "No steps yet. Add one with the Table, Column or Cell tools above the grid.")]),
    h(
      "div",
      { class: "add-step" },
      button("Save recipe", () => void saveRecipe(), { icon: "save", title: "Save these steps for this dataset name (no data is saved)" }),
    ),
  );
  for (const [index, step] of info.steps.entries()) {
    if (step.kind === "categorize" && step.enabled !== false) void categorizeStats(index);
  }
}

function countLine(index: number, final = false): HTMLElement {
  const total = counts[index];
  return h("div", { class: "step-count" }, final && total !== undefined ? `${formatCount(total)} rows in the view` : "");
}

function stepRow(step: StepJson, index: number): HTMLElement {
  const info = state.info!;
  const broken = info.step_error?.index === index;
  const blocked = info.step_error !== null && index > info.step_error.index;
  const previewing = state.previewUpto === index + 1;
  const before = counts[index];
  const after = counts[index + 1];
  const row = h(
    "li",
    {
      class: `step ${step.enabled === false ? "off" : ""} ${broken ? "broken" : ""} ${previewing ? "previewing" : ""}`,
      draggable: "true",
      ondragstart: (e: DragEvent) => { dragFrom = index; e.dataTransfer?.setData("text/plain", String(index)); },
      ondragover: (e: DragEvent) => { e.preventDefault(); row.classList.add("drag-over"); },
      ondragleave: () => row.classList.remove("drag-over"),
      ondrop: (e: DragEvent) => { e.preventDefault(); row.classList.remove("drag-over"); if (dragFrom !== null) void move(dragFrom, index); },
    },
    h("span", { class: "num" }, String(index + 1)),
    h("input", {
      type: "checkbox",
      checked: step.enabled !== false,
      title: step.enabled === false ? "Turn this step on" : "Turn this step off",
      "aria-label": `Step ${index + 1} on`,
      onchange: () => void toggle(index),
    }),
    h(
      "span",
      {
        class: "text",
        role: "button",
        tabindex: "0",
        title: "Preview the data after this step",
        onclick: () => state.setPreview(previewing ? null : index + 1),
        onkeydown: (e: KeyboardEvent) => { if (e.key === "Enter") state.setPreview(previewing ? null : index + 1); },
      },
      step.description ?? step.kind,
      step.params.header ? h("span", { class: "hint" }, " (from the column header)") : null,
      step.params.from_chart ? h("span", { class: "hint" }, " (from a chart)") : null,
      previewing ? h("span", { class: "badge preview-tag" }, "previewing") : null,
    ),
    h(
      "span",
      { class: "actions" },
      step.kind in KIND_LABELS && step.kind !== "distinct" ? button(`Edit step ${index + 1}`, () => editStep(step.kind, index), { icon: "edit", iconOnly: true, class: "ghost" }) : null,
      button(`Delete step ${index + 1}`, () => void remove(index), { icon: "trash", iconOnly: true, class: "ghost" }),
    ),
    before !== undefined && after !== undefined && before !== after
      ? h("span", { class: "meta" }, `${formatCount(before)} → ${formatCount(after)} rows`)
      : null,
    h("span", { class: "meta", "data-categorize": String(index) }),
    broken ? h("span", { class: "error-text" }, info.step_error!.message) : null,
    blocked ? h("span", { class: "meta" }, "Not applied: an earlier step is broken.") : null,
  );
  return row;
}

async function categorizeStats(index: number): Promise<void> {
  try {
    const stats = await api<{ unmatched: number; overlapping: number }>(`${grid}/categorize-stats${query({ step: index })}`);
    const slot = host.querySelector(`[data-categorize="${index}"]`);
    if (slot) {
      slot.textContent = `${formatCount(stats.unmatched)} rows matched no rule`;
      if (stats.overlapping) slot.append(h("span", { class: "warning-text" }, ` · ⚠ ${formatCount(stats.overlapping)} matched more than one (the first wins)`));
    }
  } catch {
    // Shown only when it can be computed.
  }
}

async function toggle(index: number): Promise<void> {
  const steps = [...state.info!.steps];
  const step = steps[index]!;
  steps[index] = { ...step, enabled: step.enabled === false };
  await state.saveSteps(steps).catch((e: Error) => toast(e.message, "error"));
}

async function remove(index: number): Promise<void> {
  const steps = state.info!.steps.filter((_, i) => i !== index);
  await state.saveSteps(steps).catch((e: Error) => toast(e.message, "error"));
}

async function move(from: number, to: number): Promise<void> {
  if (from === to) return;
  const steps = [...state.info!.steps];
  const [moved] = steps.splice(from, 1);
  steps.splice(to, 0, moved!);
  dragFrom = null;
  await state.saveSteps(steps).catch((e: Error) => toast(e.message, "error"));
}

async function saveRecipe(): Promise<void> {
  try {
    const result = await api<{ saved: string }>(`${grid}/recipe`, "POST");
    toast(`Recipe saved (${result.saved}). It is applied when this dataset name is pushed again.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

interface CodeResult {
  target: string;
  code: string;
  sql: string;
  reads: string;
  verified: boolean;
  difference: string | null;
  note: string | null;
  rows_checked: number;
}

/** What the user pastes into their own Python: one DuckDB query over their table by its variable
 * name ("Copy as SQL"), or `import duckdb` plus the one line that runs it ("Copy as Python").
 * pandas and plain Python are longer translations, offered second. */
async function renderCode(body: HTMLElement): Promise<void> {
  const targets: [string, string][] = [
    ["duckdb", "DuckDB SQL"],
    ["pandas", "pandas (longer translation)"],
    ["plain", "Plain Python (longer translation)"],
  ];
  const picker = h("select", { "aria-label": "Language" });
  const status = h("span", { class: "hint" }, "Generating and verifying…");
  const pre = h("pre", { class: "mono" });
  let result: CodeResult | null = null;
  const copyText = (text: string, what: string): void =>
    void navigator.clipboard.writeText(text).then(
      () => toast(`Copied the verified ${what}.`),
      () => toast("The browser blocked the clipboard. Select the text and copy it.", "error"),
    );
  const copySql = button("Copy as SQL", () => result && copyText(result.sql.trim() + "\n", "SQL query"), {
    icon: "copy",
    class: "primary",
    title: "One DuckDB query over your own table, by its variable name",
  });
  const copyPython = button("Copy as Python", () => result && copyText(result.code, "Python"), {
    icon: "copy",
    title: "import duckdb and one line that runs the query on your table",
  });
  copySql.hidden = copyPython.hidden = true;
  body.append(h("div", { class: "code-view" }, h("div", { class: "code-toolbar" }, picker, copySql, copyPython, status), pre));
  try {
    result = await api<CodeResult>(`${grid}/code${query({ target: codeTarget })}`);
    const shown = result;
    for (const [v, label] of targets) picker.append(h("option", { value: v, selected: v === shown.target }, label));
    picker.addEventListener("change", () => { codeTarget = picker.value; void render(); });
    pre.textContent = shown.code;
    if (shown.verified) {
      copySql.hidden = shown.target !== "duckdb";
      copyPython.hidden = false;
      if (shown.target !== "duckdb") copyPython.classList.add("primary");
      clear(status, h("span", { class: "badge ok" }, icon("eye"), `Verified identical on ${formatCount(shown.rows_checked)} rows`));
    } else {
      clear(status, h("span", { class: "badge error" }, "Not verified"), " ", shown.difference ?? "");
    }
    body.append(h("p", { class: "hint" }, `${shown.reads} The code embeds no data.`));
    if (shown.note) body.append(h("p", { class: "hint" }, shown.note));
  } catch (e) {
    status.textContent = (e as Error).message;
    status.className = "status-error";
  }
}

function popOut(): void {
  const page = `/g/${encodeURIComponent(gridId)}`;
  const url = `${page}?panel=steps#token=${encodeURIComponent(token)}`;
  const win = window.open(url, `steps-${page}`, "width=440,height=820");
  if (!win) toast("The browser blocked the pop-out window.", "error");
}
