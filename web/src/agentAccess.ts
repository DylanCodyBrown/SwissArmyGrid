/**
 * agentAccess.ts: what an agent may see, for every table, and its requests for a click. [STATE + WIRE]
 *
 * One privacy level covers all open tables (the server keeps one policy). It is set in the
 * Settings window, which shows agentAccessSection(). Masks are by column name, so a rule
 * follows that column into every table that has it. Below level 3 a row read can wait for the
 * user's click, and an export always does: each request opens the approval popup at once,
 * wherever the user is, rather than waiting in a tab they might not be looking at.
 *
 * ROUTING: [LEVEL CHOICE]     -> savePolicy({ level })            (0 to 3; 3 = everything, the default)
 *          [RUN AUTOMATICALLY] -> savePolicy({ auto_rows })
 *          [COLUMN MASKS]     -> savePolicy({ masks })            (levels 1 and 2)
 *          [AGENT ACTIVITY]   -> read-only, from GET /audit (every table)
 *          [APPROVAL POPUP]   Allow / Deny -> resolveApproval(id, approve)
 */
import { api, query, type Notice } from "./api";
import { button, clear, formatCount, h, icon, openDialog, toast } from "./dom";
import { contextOf, gridList } from "./panes";

interface Policy {
  level: number;
  auto_rows: boolean;
  masks: Record<string, string>;
}
interface Approval {
  id: string;
  dataset: string;
  action: string;
  detail: string;
}

const LEVELS: [number, string, string][] = [
  [3, "Level 3: everything", "Every row and column, up to 1,048,576 rows per read (one Excel sheet), with no masks and no waiting for a click."],
  [2, "Level 2: rows", "Up to 200 rows per read, with the column masks below. Each read waits for Allow unless it runs automatically."],
  [1, "Level 1: masked samples", "Up to 20 rows; text columns masked unless marked to show the value."],
  [0, "Level 0: numbers only", "Schema, counts and profile numbers. No rows, no text values."],
];

let pending: Approval[] = [];
let popup: { body: HTMLElement; close: () => void } | null = null;

// ===== the Settings section =====

/** The agent's access, for the Settings window. Redraws itself after each change. */
export function agentAccessSection(): HTMLElement {
  const host = h("div", { class: "agent-access" });
  void renderSection(host);
  return host;
}

async function renderSection(host: HTMLElement): Promise<void> {
  let policy: Policy = { level: 3, auto_rows: false, masks: {} };
  try {
    policy = await api<Policy>("/agent/policy");
  } catch {
    // the default policy
  }
  const save = (next: Partial<Policy>): void => void savePolicy({ ...policy, ...next }).then(() => renderSection(host));
  const auto = h("input", { type: "checkbox", checked: policy.auto_rows, disabled: policy.level >= 3, onchange: () => save({ auto_rows: auto.checked }) });
  clear(
    host,
    h(
      "fieldset",
      { class: "fieldset" },
      h("legend", {}, "What the agent may see (every table)"),
      h(
        "div",
        { role: "radiogroup", "aria-label": "Agent privacy level" },
        ...LEVELS.map(([level, label, text]) =>
          h(
            "label",
            { class: "choice" },
            h("input", { type: "radio", name: "agent-level", value: String(level), checked: policy.level === level, onchange: () => save({ level }) }),
            h("span", {}, h("strong", {}, label), h("small", {}, text)),
          ),
        ),
      ),
      h("label", { class: "check" }, auto, "Run row reads without asking me (levels 1 and 2; otherwise each one waits for Allow)"),
      // The one thing the level does not limit: a picture of the app is the page as you see it.
      h(
        "p",
        { class: "hint" },
        "Snapshots are the exception. When an agent asks to see the app it gets the page exactly as you see it, " +
          "cell values included, whatever level is set here. Each one is listed under Agent activity.",
      ),
    ),
    masksFieldset(policy, save),
    await activityFieldset(),
  );
}

/** Masks by column name, for every column of every open table. */
function masksFieldset(policy: Policy, save: (next: Partial<Policy>) => void): HTMLElement {
  const names = new Set<string>(Object.keys(policy.masks));
  for (const g of gridList()) for (const c of contextOf(g.id)?.state.info?.columns ?? []) names.add(c.name);
  const rows = [...names].sort((a, b) => a.localeCompare(b)).map((name) => {
    const pick = h("select", { "aria-label": `Mask for ${name}` });
    for (const [v, label] of [["", "default"], ["show", "the value"], ["hash", "a hash"], ["hide", "nothing (hidden)"]]) pick.append(h("option", { value: v, selected: (policy.masks[name] ?? "") === v }, label));
    pick.addEventListener("change", () => {
      const masks = { ...policy.masks };
      if (pick.value) masks[name] = pick.value;
      else delete masks[name];
      save({ masks });
    });
    return h("tr", {}, h("td", {}, name), h("td", {}, pick));
  });
  return h(
    "fieldset",
    { class: "fieldset" },
    h("legend", {}, "Column masks (levels 1 and 2)"),
    h("p", { class: "hint" }, "By column name: a rule applies in every table with that column."),
    rows.length
      ? h("div", { class: "masks-scroll" }, h("table", { class: "small masks" }, h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Column"), h("th", { scope: "col" }, "Agent sees"))), h("tbody", {}, ...rows)))
      : h("p", { class: "hint" }, "Open a table to set masks on its columns."),
  );
}

async function activityFieldset(): Promise<HTMLElement> {
  let entries: { time: string; actor: string; action: string; dataset: string | null; query?: string; rows?: number }[] = [];
  try {
    // The agent's own last 30: filtered from the last 200 of everyone's, they were gone once the
    // user had done 200 things since.
    entries = (await api<{ entries: typeof entries }>(`/audit${query({ limit: 30, actor: "agent" })}`)).entries.reverse();
  } catch {
    // no history
  }
  return h(
    "fieldset",
    { class: "fieldset" },
    h("legend", {}, "Agent activity"),
    entries.length
      ? h(
          "ul",
          { class: "activity" },
          ...entries.map((e) =>
            h(
              "li",
              {},
              h("span", { class: "hint" }, `${new Date(e.time).toLocaleTimeString()} `),
              `${e.action}${e.dataset ? ` on ${e.dataset}` : ""}${e.rows !== undefined ? ` (${formatCount(e.rows)} rows)` : ""}`,
              e.query ? h("div", { class: "mono hint" }, e.query) : null,
            ),
          ),
        )
      : h("p", { class: "hint" }, "No agent activity yet. Every agent action is written to the audit log (never with cell values)."),
  );
}

async function savePolicy(policy: Policy): Promise<void> {
  try {
    await api("/agent/policy", "POST", policy);
    toast(`Agent access saved for every table (level ${policy.level}).`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

// ===== approval requests =====

/** Live notices about the agent's requests: open, update or close the approval popup. */
export function handleAgentNotice(notice: Notice): void {
  if (notice.type === "approval_requested" && notice.id && !pending.some((a) => a.id === notice.id)) {
    pending.push({ id: notice.id, dataset: notice.dataset ?? "", action: notice.action ?? "", detail: notice.detail ?? "" });
    showApprovals();
  }
  if (notice.type === "approval_resolved") {
    pending = pending.filter((a) => a.id !== notice.id);
    showApprovals();
  }
}

/** On page load: any request already waiting. */
export async function loadApprovals(): Promise<void> {
  try {
    pending = (await api<{ pending: Approval[] }>("/agent/approvals")).pending;
    showApprovals();
  } catch {
    // none
  }
}

function showApprovals(): void {
  if (!pending.length) {
    popup?.close();
    popup = null;
    return;
  }
  if (!popup || !popup.body.isConnected) {
    const { dialog, body, foot, close } = openDialog("The agent is asking");
    foot.append(h("span", { class: "hint" }, "Closing this window declines the requests in it. Unanswered requests are declined after 3 minutes."));
    // Escape or the X used to hide a request that was still waiting, with nothing in the page to
    // bring it back: the agent waited out its 3 minutes. Closing now answers No.
    dialog.addEventListener("close", () => {
      for (const approval of [...pending]) void resolveApproval(approval.id, false);
    });
    popup = { body, close };
  }
  clear(
    popup.body,
    ...pending.map((approval) =>
      h(
        "section",
        { class: "approval", role: "alertdialog", "aria-label": "Agent approval request" },
        h("strong", {}, icon("agent"), ` The agent wants to ${approval.action === "query" ? "read rows with this query" : approval.action} in ${approval.dataset}:`),
        h("pre", { class: "mono" }, approval.detail),
        h(
          "div",
          { class: "row" },
          button("Allow", () => void resolveApproval(approval.id, true), { class: "primary" }),
          button("Deny", () => void resolveApproval(approval.id, false), { class: "danger" }),
        ),
      ),
    ),
  );
}

async function resolveApproval(id: string, approve: boolean): Promise<void> {
  try {
    await api(`/agent/approvals/${id}`, "POST", { approve });
    pending = pending.filter((a) => a.id !== id);
    showApprovals();
  } catch (e) {
    toast((e as Error).message, "error");
  }
}
