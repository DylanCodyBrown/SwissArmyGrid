/**
 * workbenchAgent.ts: Send to the agent, from the Workbench. [WIRE]
 *
 * Right-click an error (its panel, or the underlined code) or selected code, and choose Send to the
 * agent: it goes to the agent's inbox on this server (memory only, nothing on disk). The agent reads
 * it two ways (the owner, 2026-11-14: "it will send via hook in the MCP"):
 *   - the plugin's UserPromptSubmit hook (swissarmygrid-context) puts what is waiting into the user's
 *     next message to the agent, so "why does this fail?" needs no copying;
 *   - the MCP tool agent_inbox reads it at any time.
 * "…with a question" adds the user's own words to it.
 *
 * ROUTING: POST /agent/inbox {kind, title, text, note}
 */
import { api } from "./api";
import { button, h, openDialog, toast } from "./dom";
import type { ErrorDetail } from "./workbenchErrors";

export interface AgentItem {
  kind: "error" | "code";
  /** Where it came from: "rates.py, lines 3 to 5", "cell 2 of analysis.ipynb". */
  title: string;
  text: string;
}

let documentName: () => string = () => "the Workbench";

/** The page says which document is in front (its name goes with what is sent). */
export function setDocumentName(name: () => string): void {
  documentName = name;
}

export function currentDocument(): string {
  return documentName();
}

async function post(item: AgentItem, note: string): Promise<void> {
  try {
    await api("/agent/inbox", "POST", { ...item, note });
    toast("Sent to your agent: it sees it with your next message (or with its agent_inbox tool).");
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

/** Send now, or (withQuestion) ask for the user's own words first. */
export function sendToAgent(item: AgentItem, withQuestion = false): void {
  if (!withQuestion) return void post(item, "");
  const { body, foot, close } = openDialog("Send to the agent");
  const note = h("textarea", { rows: 4, class: "wb-agent-note", placeholder: "What should it do? (Why does this fail? Make it faster…)", "aria-label": "Your question" }) as HTMLTextAreaElement;
  body.append(h("p", { class: "hint" }, item.title), h("pre", { class: "wb-agent-preview mono" }, item.text.slice(0, 1200) + (item.text.length > 1200 ? "\n…" : "")), note);
  foot.append(
    button("Cancel", close, { class: "ghost" }),
    button("Send", () => {
      close();
      void post(item, note.value.trim());
    }, { icon: "agent", class: "primary" }),
  );
  setTimeout(() => note.focus(), 0);
}

/** An error as the agent reads it: what, where, the plain words, the values, the code, the traceback's end. */
export function errorText(detail: ErrorDetail, traceback: string): string {
  const last = detail.chain[detail.chain.length - 1];
  if (!last) return traceback;
  const lines = [`${last.type}${last.message ? `: ${last.message}` : ""}`];
  if (detail.where) lines.push(`Where: ${detail.where}`);
  if (last.hint) lines.push(`Python's hint: ${last.hint}`);
  if (last.explanation) lines.push(`What it means: ${last.explanation}`);
  const at = last.your_frame !== null && last.your_frame !== undefined ? last.frames[last.your_frame] : undefined;
  if (at?.code) lines.push(`The failing line: ${at.code.trim()}`);
  if (at?.values.length) lines.push(`Values at that moment: ${at.values.map((v) => `${v.expr} = ${v.repr} (${v.type})`).join("; ")}`);
  if (at?.locals.length) lines.push(`${at.function}()'s variables: ${at.locals.map((v) => `${v.name} = ${v.repr}`).join("; ")}`);
  if (last.fixes.length) lines.push(`Fixes the Workbench offered: ${last.fixes.map((f) => f.title.replace(/`/g, "")).join("; ")}`);
  for (const earlier of detail.chain.slice(0, -1)) lines.push(`It followed: ${earlier.type}: ${earlier.message}`);
  const plain = traceback.replace(/\u001b\[[0-9;]*[A-Za-z]/g, "").trim();
  if (plain) lines.push("Traceback (its end):", plain.slice(-3000));
  return lines.join("\n");
}

/** What the editor's right-click sends: the code, where it is, and the error marked there if any. */
export function codeItem(where: string, what: { text: string; problem: { message: string; hover: string } | null }, language: string): AgentItem {
  const fence = "```";
  const code = `${fence}${language}\n${what.text.replace(/```/g, "ˋˋˋ")}\n${fence}`;
  if (what.problem) {
    const plain = what.problem.hover.replace(/\*\*|_|`/g, "");
    return { kind: "error", title: `An error in ${where}`, text: `${what.problem.message}\n${plain}\nThe code there:\n${code}` };
  }
  return { kind: "code", title: `Code from ${where}`, text: code };
}

/** A small menu at the pointer (right-click): each choice runs and the menu goes. */
export function contextMenu(event: MouseEvent, items: { label: string; run: () => void }[]): void {
  event.preventDefault();
  document.querySelector(".menu")?.remove();
  const menu = h("div", { class: "menu", role: "menu" }, ...items.map((i) => h("button", { type: "button", role: "menuitem", onclick: () => { menu.remove(); i.run(); } }, i.label)));
  document.body.append(menu);
  menu.style.top = `${Math.min(event.clientY, innerHeight - menu.offsetHeight - 8)}px`;
  menu.style.left = `${Math.min(event.clientX, innerWidth - menu.offsetWidth - 8)}px`;
  (menu.querySelector("button") as HTMLButtonElement | null)?.focus();
  const away = (e: Event): void => {
    if (!menu.contains(e.target as Node)) {
      menu.remove();
      document.removeEventListener("mousedown", away);
    }
  };
  setTimeout(() => document.addEventListener("mousedown", away));
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape") menu.remove();
  });
}
