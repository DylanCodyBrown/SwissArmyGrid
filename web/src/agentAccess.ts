/**
 * agentAccess.ts: what the agent has done, and its requests for a click. [STATE + WIRE]
 *
 * The agent sees what the page shows. An agent export always waits for the user's click: each
 * request opens the approval popup at once, wherever the user is, rather than waiting in a tab
 * they might not be looking at. The Settings window shows agentAccessSection(): the agent's
 * recent actions, from the audit log.
 *
 * ROUTING: [AGENT ACTIVITY]   -> read-only, from GET /audit (every table)
 *          [APPROVAL POPUP]   Allow / Deny -> resolveApproval(id, approve)
 */
import { api, query, type Notice } from "./api";
import { button, clear, formatCount, h, icon, openDialog, toast } from "./dom";

interface Approval {
  id: string;
  dataset: string;
  action: string;
  detail: string;
}

let pending: Approval[] = [];
let popup: { body: HTMLElement; close: () => void } | null = null;

// ===== the Settings section =====

/** The agent's access, for the Settings window: what it sees, and what it has done. */
export function agentAccessSection(): HTMLElement {
  const host = h("div", { class: "agent-access" });
  void activityFieldset().then((activity) => clear(host, activity));
  return host;
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
      : h("p", { class: "hint" }, "No agent activity"),
  );
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
    const { dialog, body, foot, close } = openDialog("The agent is asking", true);
    foot.append(h("span", { class: "hint" }, "Closing declines. Expires in 3 minutes."));
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
        h("strong", {}, icon("agent"), ` The agent wants to ${approval.action} ${approval.dataset}:`),
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
