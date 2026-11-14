/**
 * toPython.ts: hand code to the user's own Python (server: routes_handoff.py). The code is copied to
 * the clipboard and queued: in Jupyter or IPython, `sag.paste()` puts the newest in the cell (or at the
 * prompt), imports first. Nothing is run for the user.
 */
import { api } from "./api";
import { toast } from "./dom";

interface Sent {
  code: string;
  waiting: number;
}

async function copied(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false; // the browser may refuse; sag.paste() still has it
  }
}

function told(sent: Sent, clipboard: boolean): void {
  toast(clipboard ? "Copied for sag.paste()." : "Queued for sag.paste().");
}

/** Code (and the imports it needs) for the user's Python. */
export async function toPython(code: string, imports: string[], label: string): Promise<void> {
  try {
    const sent = await api<Sent>("/python/send", "POST", { code, imports, label });
    told(sent, await copied(sent.code));
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

