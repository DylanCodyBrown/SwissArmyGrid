/**
 * renameTable.ts: call a table by another name (the usability test, 2026-11-14). [WIRE]
 *
 *   [TOAST]   a pasted table's toast holds its name in a box: type a better one, Enter renames it
 *   [IN PLACE] a double-click (or F2) on a tab's name turns it into a box: Enter renames, Escape leaves it
 *
 * Every window follows the server's dataset_renamed notice (panes.ts renameContexts), so this only asks.
 *
 * ROUTING: POST /datasets/{name}/rename {to}
 */
import { api } from "./api";
import { h, toast } from "./dom";

/** Ask the server to rename `name`; a refusal (a taken or bad name) is a toast. */
export async function renameTable(name: string, to: string): Promise<boolean> {
  const next = to.trim();
  if (!next || next === name) return false;
  try {
    await api(`/datasets/${encodeURIComponent(name)}/rename`, "POST", { to: next });
    return true;
  } catch (e) {
    toast((e as Error).message, "error");
    return false;
  }
}

/** A box over `label` holding `name`, all selected: Enter (or leaving it) renames, Escape cancels. */
export function renameInPlace(label: HTMLElement, name: string): void {
  if (label.querySelector("input")) return;
  const was = label.textContent ?? name;
  const box = h("input", { class: "tab-rename", value: name, "aria-label": `New name for ${name}`, spellcheck: "false" }) as HTMLInputElement;
  let done = false;
  const finish = (keep: boolean): void => {
    if (done) return;
    done = true;
    const to = box.value;
    label.textContent = was;
    if (keep) void renameTable(name, to);
  };
  box.addEventListener("keydown", (e) => {
    e.stopPropagation(); // the tab's own keys (Delete closes it, arrows move) are not for the box
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  box.addEventListener("blur", () => finish(true));
  for (const kind of ["click", "pointerdown", "dblclick"]) box.addEventListener(kind, (e) => e.stopPropagation());
  label.replaceChildren(box);
  box.focus();
  box.select();
}

/** The Pasted toast: its name in a box, ready for a better one. It stays while the box is in use. */
export function pastedToast(name: string): void {
  let host = document.querySelector<HTMLElement>(".toasts");
  if (!host) {
    host = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(host);
  }
  const box = h("input", { class: "toast-name", value: name, "aria-label": `Name for ${name}`, spellcheck: "false" }) as HTMLInputElement;
  const item = h("div", { class: "toast toast-naming" }, h("span", {}, "Pasted as"), box, h("span", { class: "muted" }, "Enter renames"));
  host.append(item);
  let timer = 0;
  const later = (ms: number): void => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => item.remove(), ms);
  };
  box.addEventListener("focus", () => window.clearTimeout(timer));
  box.addEventListener("blur", () => later(4000));
  box.addEventListener("keydown", (e) => {
    if (e.key === "Escape") item.remove();
    if (e.key !== "Enter") return;
    void renameTable(name, box.value).then((ok) => {
      if (ok) item.remove();
    });
  });
  later(8000);
}
