/**
 * workbenchKernel.ts: a document's IPython kernel, and how a run's outputs are drawn. [WIRE + FUNCTION]
 *
 * ROUTING: [START] POST /workbench/kernels {python, path}
 *          [RUN]   POST /workbench/kernels/{id}/execute: read as it streams, one JSON output a line
 *          [STOP]  /interrupt, /restart, DELETE (on closing the document or the window)
 *
 * Outputs are nbformat's own records, so a notebook saves them as Jupyter would. Drawn here:
 *   stream     text (stdout; stderr marked), consecutive pieces joined
 *   error      the traceback, its colour codes taken out
 *   data       a picture (PNG, JPEG, SVG as an image), Plotly, Markdown, HTML (a DataFrame) in a frame
 *              that runs no script and loads nothing, else the plain text
 */
import { api, ApiError, token } from "./api";
import { clear, h } from "./dom";
import { renderMarkdown } from "./markdown";

export type Output = Record<string, unknown> & { output_type?: string };
export interface Done {
  ok: boolean;
  execution_count: number | null;
  dead?: boolean;
  error?: string;
}

export class Kernel {
  id: string | null = null;
  python = "";
  state: "off" | "starting" | "idle" | "busy" = "off";
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
      const started = await api<{ kernel: string; python: string }>("/workbench/kernels", "POST", { python: this.chosenPython(), path: this.path() });
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
  async run(body: { code: string; kind?: string; connection?: string | null }, onOutput: (o: Output) => void): Promise<Done> {
    const id = await this.ensure();
    this.set("busy");
    try {
      const response = await fetch(`/workbench/kernels/${id}/execute`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (response.status === 404) {
        this.id = null; // the server restarted or the kernel was stopped: start a new one next time
        throw new ApiError(404, "The kernel was gone; run again to start a new one.");
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
          const item = JSON.parse(line) as Output & { done?: Done };
          if (item.done) done = item.done;
          else onOutput(item);
        }
        if (finished) break;
      }
      if (done.dead) this.id = null;
      return done;
    } finally {
      this.set(this.id ? "idle" : "off");
    }
  }

  async interrupt(): Promise<void> {
    if (this.id) await api(`/workbench/kernels/${this.id}/interrupt`, "POST");
  }

  async restart(): Promise<void> {
    if (!this.id) return;
    this.set("starting");
    try {
      await api(`/workbench/kernels/${this.id}/restart`, "POST");
    } finally {
      this.set("idle");
    }
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

// ===== drawing outputs =====

const ANSI = /\u001b\[[0-9;]*[A-Za-z]/g;
const text = (value: unknown): string => (Array.isArray(value) ? value.join("") : typeof value === "string" ? value : "");

let plotly: Promise<typeof import("./lazy/plotly")> | null = null;

/** HTML (a DataFrame, a styled table) in a frame that runs no script and loads nothing from outside. */
function htmlFrame(html: string, dark: boolean): HTMLIFrameElement {
  const frame = h("iframe", { class: "wb-html", sandbox: "allow-same-origin", title: "Output", loading: "lazy" }) as HTMLIFrameElement;
  const style = `<style>body{margin:0;font:13px "Segoe UI",system-ui,sans-serif;color:${dark ? "#e8ecef" : "#1c2228"};background:transparent}
table{border-collapse:collapse}th,td{padding:3px 8px;border-bottom:1px solid ${dark ? "#343c45" : "#d5dbe1"};text-align:right}
th{font-weight:600}thead th{border-bottom-width:2px}tbody tr:hover{background:${dark ? "#21272d" : "#f2f4f7"}}</style>`;
  frame.srcdoc = `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">${style}${html}`;
  frame.addEventListener("load", () => {
    // The content's own height (the page's grows to the frame's, so it can't shrink to fit).
    const body = frame.contentDocument?.body;
    if (body) frame.style.height = `${Math.min(Math.ceil(body.getBoundingClientRect().height) + 6, 640)}px`;
  });
  return frame;
}

function drawData(data: Record<string, unknown>, dark: boolean): HTMLElement {
  const png = data["image/png"];
  if (typeof png === "string") return h("img", { class: "wb-image", src: `data:image/png;base64,${png.trim()}`, alt: "Figure" });
  const jpeg = data["image/jpeg"];
  if (typeof jpeg === "string") return h("img", { class: "wb-image", src: `data:image/jpeg;base64,${jpeg.trim()}`, alt: "Figure" });
  const svg = data["image/svg+xml"];
  if (svg) return h("img", { class: "wb-image", src: `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(text(svg))))}`, alt: "Figure" });
  const figure = data["application/vnd.plotly.v1+json"];
  if (figure) {
    const slot = h("div", { class: "wb-plotly" });
    plotly ??= import(/* external */ "/static/build/lazy/plotly.js" as string) as Promise<typeof import("./lazy/plotly")>;
    void plotly.then((m) => m.drawPlotly(slot, JSON.stringify(figure), dark)).catch((e: Error) => (slot.textContent = e.message));
    return slot;
  }
  const markdown = data["text/markdown"];
  if (markdown) return h("div", { class: "wb-markdown" }, renderMarkdown(text(markdown)));
  const html = data["text/html"];
  if (html) return htmlFrame(text(html), dark);
  return h("pre", { class: "wb-text" }, text(data["text/plain"]));
}

/** Draw `outputs` into `host` (its old content replaced). */
export function drawOutputs(host: HTMLElement, outputs: Output[], dark: boolean): void {
  const parts: HTMLElement[] = [];
  let last: { name: string; pre: HTMLElement } | null = null;
  for (const o of outputs) {
    if (o.output_type === "stream") {
      const name = String(o.name ?? "stdout");
      if (last && last.name === name) {
        last.pre.textContent += text(o.text);
        continue;
      }
      const pre: HTMLElement = h("pre", { class: `wb-stream ${name}` }, text(o.text));
      last = { name, pre };
      parts.push(pre);
      continue;
    }
    last = null;
    if (o.output_type === "error") {
      const traceback = (o.traceback as string[] | undefined)?.join("\n") || `${String(o.ename)}: ${String(o.evalue)}`;
      parts.push(h("pre", { class: "wb-error" }, traceback.replace(ANSI, "")));
    } else if (o.output_type === "execute_result" || o.output_type === "display_data") {
      parts.push(drawData((o.data as Record<string, unknown>) ?? {}, dark));
    }
  }
  clear(host, ...parts);
}
