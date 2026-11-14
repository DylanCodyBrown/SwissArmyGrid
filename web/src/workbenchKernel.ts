/**
 * workbenchKernel.ts: a document's IPython kernel, and how a run's outputs are drawn. [WIRE + FUNCTION]
 *
 * ROUTING: [START] POST /workbench/kernels {python, path}: it starts with every vault's variables set
 *          [RUN]   POST /workbench/kernels/{id}/execute: read as it streams, one JSON output a line
 *          [STOP]  /interrupt, /restart, DELETE (on closing the document or the window)
 *
 *          [INPUT] POST /workbench/kernels/{id}/input {value}: the line input() asked for
 *
 * Outputs are nbformat's own records (drawn by workbenchOutputs.ts). The state is "running" from the
 * first Run until the last queued run ends (two runs sent at once run one after the other).
 */
import { api, ApiError, token } from "./api";
import { toast } from "./dom";
import type { Output, OutputArea } from "./workbenchOutputs";

export type { Output } from "./workbenchOutputs";
export { drawOutputs } from "./workbenchOutputs";

export interface Done {
  ok: boolean;
  execution_count: number | null;
  /** The kernel process ended (out of memory, os._exit): the next Run starts a new one. */
  dead?: boolean;
  /** Restart ended this run. */
  restarted?: boolean;
  /** Interrupt cancelled it before it started. */
  cancelled?: boolean;
  error?: string;
}

/** What a run sends: code (Python, or SQL on a connection), or a file run as Python would run it. */
export interface RunBody {
  code: string;
  kind?: "code" | "sql" | "file";
  connection?: string | null;
  path?: string | null;
  args?: string;
}

/** An item of a run's stream: an output record, or the kernel asking for a line. */
export type RunItem = Output & { input_request?: { prompt: string; password: boolean } };

/** This window, for this page load: its kernels stop a minute after it is gone (closed or reloaded). */
export const WINDOW_ID: string = Math.random().toString(36).slice(2, 12);

export class Kernel {
  id: string | null = null;
  python = "";
  state: "off" | "starting" | "idle" | "busy" = "off";
  /** Runs sent and not yet ended (a second Run waits behind the first). */
  running = 0;
  private listeners: (() => void)[] = [];

  constructor(private readonly path: () => string | null, private readonly chosenPython: () => string | null) {}

  onState(listener: () => void): void {
    this.listeners.push(listener);
  }

  private set(state: Kernel["state"]): void {
    this.state = state;
    for (const l of this.listeners) l();
  }

  async ensure(): Promise<string> {
    if (this.id) return this.id;
    this.set("starting");
    try {
      const started = await api<{ kernel: string; python: string; vaults?: { set: string[]; problems: string[] } }>("/workbench/kernels", "POST", { python: this.chosenPython(), path: this.path(), window: WINDOW_ID });
      // A vault that could not be read: the kernel started without its variables.
      for (const problem of started.vaults?.problems ?? []) toast(problem, "error");
      this.id = started.kernel;
      this.python = started.python;
      this.set("idle");
      return started.kernel;
    } catch (e) {
      this.set("off");
      throw e;
    }
  }

  /** Run code; each output as it arrives; the run's end. */
  async run(body: RunBody, onOutput: (o: RunItem) => void): Promise<Done> {
    const id = await this.ensure();
    this.running++;
    this.set("busy");
    try {
      const response = await fetch(`/workbench/kernels/${id}/execute`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (response.status === 404) {
        this.id = null; // the server restarted or the kernel was stopped: start a new one next time
        throw new ApiError(404, "The kernel was gone: run again.");
      }
      if (!response.ok || !response.body) throw new ApiError(response.status, await response.text());
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      let done: Done = { ok: false, execution_count: null };
      for (;;) {
        const { value, done: finished } = await reader.read();
        if (value) pending += decoder.decode(value, { stream: true });
        let at: number;
        while ((at = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, at).trim();
          pending = pending.slice(at + 1);
          if (!line) continue;
          const item = JSON.parse(line) as RunItem & { done?: Done };
          if (item.done) done = item.done;
          else onOutput(item);
        }
        if (finished) break;
      }
      if (done.dead) this.id = null;
      return done;
    } finally {
      this.running = Math.max(0, this.running - 1);
      this.set(!this.id ? "off" : this.running ? "busy" : "idle");
    }
  }

  /** The line input() (or getpass, pdb) asked for. */
  async answer(value: string): Promise<void> {
    if (this.id) await api(`/workbench/kernels/${this.id}/input`, "POST", { value });
  }

  /** Whether the console's code is a whole statement yet: "complete", "incomplete" (with the next
   *  line's indent) or "invalid". Before the kernel starts, a guess from the last line. */
  async isComplete(code: string): Promise<{ status: string; indent?: string }> {
    if (this.id && this.state !== "starting") {
      try {
        return await api(`/workbench/kernels/${this.id}/is-complete`, "POST", { code });
      } catch {
        /* the guess below */
      }
    }
    const last = code.trimEnd().split("\n").pop() ?? "";
    const open = (code.match(/[([{]/g)?.length ?? 0) > (code.match(/[)\]}]/g)?.length ?? 0);
    return /[:\\]$/.test(last) || open ? { status: "incomplete", indent: "    " } : { status: "complete" };
  }

  /** The kernel's completions at `offset` (nothing before it has started: typing never starts one). */
  async complete(code: string, offset: number): Promise<{ matches: string[]; start: number; end: number; types: Record<string, string> } | null> {
    if (!this.id || this.state === "starting") return null;
    try {
      return await api(`/workbench/kernels/${this.id}/complete`, "POST", { code, offset });
    } catch {
      return null;
    }
  }

  /** The signature and help of the name at `offset`. */
  async help(code: string, offset: number): Promise<string> {
    if (!this.id || this.state === "starting") return "";
    try {
      return (await api<{ help: string }>(`/workbench/kernels/${this.id}/inspect`, "POST", { code, offset })).help;
    } catch {
      return "";
    }
  }

  async interrupt(): Promise<void> {
    if (this.id) await api(`/workbench/kernels/${this.id}/interrupt`, "POST");
  }

  /** A fresh Python. False: there was no kernel to restart (none has started yet). */
  async restart(): Promise<boolean> {
    if (!this.id) return false;
    this.set("starting");
    try {
      await api(`/workbench/kernels/${this.id}/restart`, "POST");
    } finally {
      this.set(this.running ? "busy" : "idle");
    }
    return true;
  }

  /** Stop it (the document closed). `leaving`: the window is closing, so the request must outlive it. */
  stop(leaving = false): void {
    if (!this.id) return;
    const id = this.id;
    this.id = null;
    void fetch(`/workbench/kernels/${id}`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` }, keepalive: leaving }).catch(() => undefined);
    this.set("off");
  }
}

/** Run into an output area: outputs as they come, a box when the code asks for a line, and a plain
 *  word when the run did not end the usual way (the kernel stopped, Restart, Interrupt before it ran). */
export async function runInto(kernel: Kernel, body: RunBody, area: OutputArea): Promise<Done> {
  try {
    const done = await kernel.run(body, (item) => {
      const asked = item.input_request;
      if (asked) area.ask(asked.prompt, asked.password, (value) => kernel.answer(value).catch((e: Error) => area.notice(e.message, "error")));
      else area.add(item);
    });
    area.finish();
    if (done.dead) area.notice("The kernel stopped: every variable is gone.", "error");
    else if (done.restarted) area.notice("Ended by Restart");
    else if (done.cancelled) area.notice("Cancelled");
    return done;
  } catch (e) {
    area.finish();
    area.notice((e as Error).message, "error");
    return { ok: false, execution_count: null, error: (e as Error).message };
  }
}
