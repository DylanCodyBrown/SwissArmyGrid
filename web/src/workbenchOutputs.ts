/**
 * workbenchOutputs.ts: a run's outputs, drawn as they arrive, and kept as nbformat's records. [FUNCTION]
 *
 * One OutputArea per notebook cell and per console entry. It keeps the records (what a notebook saves)
 * and draws them incrementally: new text is added to what is on screen, never redrawn from the start,
 * and drawing happens at most every 30 milliseconds however fast the kernel prints (the stress test froze
 * the page with 5,000 small prints when every record redrew the run).
 *
 *   stream     text, consecutive pieces of stdout (or stderr) joined; colour codes as colours, a `\r`
 *              writes over its line (progress bars); the newest 5,000 lines show, the rest are counted
 *   error      the traceback in its colours; a long one folds its middle frames
 *   data       a picture, Plotly, Markdown, HTML (a DataFrame) in a frame that runs no script and loads
 *              nothing, else the plain text; after 100 the rest are counted, not drawn. A picture or
 *              chart has Send to Sandbox (the Sandbox is where they gather: POST /cards)
 *   input      the kernel asking for a line (input(), getpass, pdb): a box in place, answered with Enter
 *   notice     the page's own word (the kernel stopped, restarted, the run was cancelled): never saved
 *   error_detail  the error panel (workbenchErrors.ts) in place of the plain traceback: never saved (the
 *              notebook keeps Jupyter's own error record)
 */
import { api } from "./api";
import { button, h, toast } from "./dom";
import { renderMarkdown } from "./markdown";
import { currentDocument } from "./workbenchAgent";
import { type ErrorActions, type ErrorDetail, errorPanel } from "./workbenchErrors";

/** What a failed run's error panel can do, and who is told it was shown (to mark the editor). */
export interface ErrorHooks extends ErrorActions {
  shown?: (detail: ErrorDetail) => void;
}

export type Output = Record<string, unknown> & { output_type?: string };

const MAX_LINES = 5_000; // lines of one stream block on screen
const MAX_RICH = 100; // pictures, tables and charts drawn in one area
const MAX_LINE_CHARS = 20_000; // a line without a newline this long is shown as it stands
const MAX_KEPT_CHARS = 1_000_000; // a stream's text kept for saving: the newest million characters
const MAX_KEPT_OUTPUTS = 200; // records kept for saving (the server's limit per cell)
const FOLD_FRAMES = 24; // a traceback longer than this shows its first and last frames
const FLUSH_MS = 30;

const text = (value: unknown): string => (Array.isArray(value) ? value.join("") : typeof value === "string" ? value : "");

// ===== colour codes (ANSI SGR) =====

interface Style {
  fg: string | null; // a class (ansi-fg-1) or a colour (rgb(...))
  bg: string | null;
  bold: boolean;
  dim: boolean;
  italic: boolean;
  underline: boolean;
}
const plain = (): Style => ({ fg: null, bg: null, bold: false, dim: false, italic: false, underline: false });
const ESCAPE = /\u001b\[([0-9;?]*)([A-Za-z])|\u001b[()][A-Za-z0-9]|\u001b[=>]/g;

function xterm256(n: number): string {
  if (n < 16) return `c${n}`;
  if (n >= 232) {
    const v = 8 + (n - 232) * 10;
    return `rgb(${v},${v},${v})`;
  }
  const i = n - 16;
  const step = (k: number): number => (k === 0 ? 0 : 55 + k * 40);
  return `rgb(${step(Math.floor(i / 36))},${step(Math.floor(i / 6) % 6)},${step(i % 6)})`;
}

function applySgr(style: Style, params: string): void {
  const codes = params === "" ? [0] : params.split(";").map((p) => Number(p) || 0);
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i]!;
    if (c === 0) Object.assign(style, plain());
    else if (c === 1) style.bold = true;
    else if (c === 2) style.dim = true;
    else if (c === 3) style.italic = true;
    else if (c === 4) style.underline = true;
    else if (c === 22) style.bold = style.dim = false;
    else if (c === 23) style.italic = false;
    else if (c === 24) style.underline = false;
    else if (c >= 30 && c <= 37) style.fg = `c${c - 30}`;
    else if (c >= 90 && c <= 97) style.fg = `c${c - 90 + 8}`;
    else if (c === 39) style.fg = null;
    else if (c >= 40 && c <= 47) style.bg = `c${c - 40}`;
    else if (c >= 100 && c <= 107) style.bg = `c${c - 100 + 8}`;
    else if (c === 49) style.bg = null;
    else if ((c === 38 || c === 48) && codes[i + 1] === 5) {
      const colour = xterm256(codes[i + 2] ?? 0);
      if (c === 38) style.fg = colour;
      else style.bg = colour;
      i += 2;
    } else if ((c === 38 || c === 48) && codes[i + 1] === 2) {
      const colour = `rgb(${codes[i + 2] ?? 0},${codes[i + 3] ?? 0},${codes[i + 4] ?? 0})`;
      if (c === 38) style.fg = colour;
      else style.bg = colour;
      i += 4;
    }
  }
}

function span(piece: string, style: Style): Node {
  if (!style.fg && !style.bg && !style.bold && !style.dim && !style.italic && !style.underline) return document.createTextNode(piece);
  const el = document.createElement("span");
  el.textContent = piece;
  const classes: string[] = [];
  if (style.fg?.startsWith("c")) classes.push(`ansi-fg-${style.fg.slice(1)}`);
  else if (style.fg) el.style.color = style.fg;
  if (style.bg?.startsWith("c")) classes.push(`ansi-bg-${style.bg.slice(1)}`);
  else if (style.bg) el.style.backgroundColor = style.bg;
  if (style.bold) classes.push("ansi-bold");
  if (style.dim) classes.push("ansi-dim");
  if (style.italic) classes.push("ansi-italic");
  if (style.underline) classes.push("ansi-underline");
  el.className = classes.join(" ");
  return el;
}

/** One line (no newline in it) drawn with its colours; `\r` starts the line over (a progress bar's
 *  next state), keeping the colour in effect. `style` is the state at the line's start, and is left as
 *  the state at its end. */
function drawLine(line: string, style: Style): Node[] {
  let parts: { piece: string; style: Style }[] = [];
  let at = 0;
  const add = (piece: string): void => {
    const pieces = piece.split("\r");
    pieces.forEach((p, i) => {
      if (i > 0) parts = []; // carriage return: what follows writes over the line
      if (p) parts.push({ piece: p, style: { ...style } });
    });
  };
  ESCAPE.lastIndex = 0;
  for (let m = ESCAPE.exec(line); m; m = ESCAPE.exec(line)) {
    add(line.slice(at, m.index));
    if (m[2] === "m") applySgr(style, m[1] ?? "");
    at = ESCAPE.lastIndex;
  }
  add(line.slice(at));
  return parts.map((p) => span(p.piece, p.style));
}

/** Colour codes taken out (for a title, a copy, a search). */
export const plainText = (value: string): string => value.replace(ESCAPE, "");

/** Text with colour codes, drawn into a <pre> (a traceback, a whole stream at once). */
function colouredText(value: string): DocumentFragment {
  const out = document.createDocumentFragment();
  const style = plain();
  const lines = value.replace(/\r\n/g, "\n").split("\n");
  lines.forEach((line, i) => {
    out.append(...drawLine(line, style));
    if (i < lines.length - 1) out.append("\n");
  });
  return out;
}

// ===== rich data =====

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

/** A picture or chart as a Sandbox card: an image (PNG, JPEG, SVG) or a live Plotly figure. */
function sandboxCard(data: Record<string, unknown>): { kind: string; body: string } | null {
  const figure = data["application/vnd.plotly.v1+json"];
  if (figure) return { kind: "plotly", body: JSON.stringify(figure) };
  const png = data["image/png"];
  if (typeof png === "string") return { kind: "image", body: `data:image/png;base64,${png.trim()}` };
  const jpeg = data["image/jpeg"];
  if (typeof jpeg === "string") return { kind: "image", body: `data:image/jpeg;base64,${jpeg.trim()}` };
  const svg = data["image/svg+xml"];
  if (svg) return { kind: "image", body: `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(text(svg))))}` };
  return null;
}

function withSandbox(drawn: HTMLElement, data: Record<string, unknown>): HTMLElement {
  const card = sandboxCard(data);
  if (!card) return drawn;
  const send = button("Send to Sandbox", () => {
    void api("/cards", "POST", { ...card, title: `From ${currentDocument()}` }).then(
      () => toast("Sent to the Sandbox"),
      (e: Error) => toast(e.message, "error"),
    );
  }, { icon: "export", class: "ghost wb-to-sandbox" });
  return h("figure", { class: "wb-figure" }, drawn, send);
}

function drawData(data: Record<string, unknown>, dark: boolean): HTMLElement {
  return withSandbox(drawPlain(data, dark), data);
}

function drawPlain(data: Record<string, unknown>, dark: boolean): HTMLElement {
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
  const pre = h("pre", { class: "wb-text" });
  pre.append(colouredText(text(data["text/plain"])));
  return pre;
}

/** A traceback in its colours; a long one (a RecursionError) shows its first and last frames, the
 *  middle behind a button. */
function drawError(o: Output): HTMLElement {
  const frames = (o.traceback as string[] | undefined) ?? [];
  const pre = h("pre", { class: "wb-error" });
  if (!frames.length) {
    pre.textContent = `${String(o.ename ?? "Error")}: ${String(o.evalue ?? "")}`;
    return pre;
  }
  if (frames.length <= FOLD_FRAMES) {
    pre.append(colouredText(frames.join("\n")));
    return pre;
  }
  const head = frames.slice(0, FOLD_FRAMES / 2);
  const middle = frames.slice(FOLD_FRAMES / 2, -FOLD_FRAMES / 2);
  const tail = frames.slice(-FOLD_FRAMES / 2);
  const more = h("button", { type: "button", class: "link wb-fold" }, `Show ${middle.length.toLocaleString()} more frames`);
  more.addEventListener("click", () => more.replaceWith(colouredText(`${middle.join("\n")}\n`)));
  pre.append(colouredText(`${head.join("\n")}\n`), more, "\n", colouredText(tail.join("\n")));
  return pre;
}

// ===== the area =====

interface Block {
  name: string;
  pre: HTMLElement;
  /** The line not yet ended by a newline: drawn again as text arrives. */
  tail: HTMLElement;
  current: string;
  style: Style; // the colour in effect at the start of the current line
  lines: number;
  hidden: number;
  note: HTMLElement | null;
}

export interface OutputArea {
  element: HTMLElement;
  /** The records, as a notebook saves them. */
  outputs: () => Output[];
  /** Show these records instead (a notebook opened, a run started). */
  set: (outputs: Output[]) => void;
  /** One record from the kernel: kept and drawn (soon: drawing is batched). */
  add: (o: Output) => void;
  /** The page's own word under the outputs (not saved). */
  notice: (message: string, kind?: "info" | "error") => void;
  /** The kernel asks for a line: a box in place; `send` hands the answer back. */
  ask: (prompt: string, password: boolean, send: (value: string) => Promise<void>) => void;
  /** The run ended: draw what is waiting, close a box still asking. */
  finish: () => void;
}

export function outputArea(dark: () => boolean, grew: () => void = () => undefined, errors?: ErrorHooks): OutputArea {
  const element = h("div", { class: "wb-outputs", "aria-live": "polite" });
  let kept: Output[] = [];
  let waiting: Output[] = [];
  let timer: number | null = null;
  let block: Block | null = null;
  let rich = 0;
  let notDrawn = 0;
  let notDrawnNote: HTMLElement | null = null;
  let clearOnNext = false;
  let asking: { row: HTMLElement; done: () => void } | null = null;
  let lastError: HTMLElement | null = null; // the traceback the error panel takes in

  const keep = (o: Output): void => {
    const last = kept[kept.length - 1];
    if (o.output_type === "stream" && last?.output_type === "stream" && last.name === o.name) {
      let joined = text(last.text) + text(o.text);
      if (joined.length > MAX_KEPT_CHARS) joined = joined.slice(-MAX_KEPT_CHARS);
      last.text = joined;
      return;
    }
    if (kept.length < MAX_KEPT_OUTPUTS) kept.push(o.output_type === "stream" ? { ...o, text: text(o.text) } : o);
  };

  const reset = (): void => {
    element.replaceChildren();
    lastError = null;
    block = null;
    rich = 0;
    notDrawn = 0;
    notDrawnNote = null;
  };

  const newBlock = (name: string): Block => {
    const pre = h("pre", { class: `wb-stream ${name}` });
    const tail = h("span", { class: "wb-tail" });
    pre.append(tail);
    element.append(pre);
    return { name, pre, tail, current: "", style: plain(), lines: 0, hidden: 0, note: null };
  };

  /** Stream text added to the block on screen: whole lines go in once; the unfinished one is redrawn. */
  const write = (b: Block, incoming: string): void => {
    const all = (b.current + incoming).replace(/\r\n/g, "\n");
    const pieces = all.split("\n");
    let rest = pieces.pop() ?? "";
    const done = document.createDocumentFragment();
    let added = 0;
    for (const line of pieces) {
      const row = document.createElement("span");
      row.className = "wb-line";
      row.append(...drawLine(line, b.style), "\n");
      done.append(row);
      added++;
    }
    if (rest.length > MAX_LINE_CHARS) {
      const row = document.createElement("span");
      row.className = "wb-line";
      row.append(...drawLine(rest, b.style), "\n");
      done.append(row);
      added++;
      rest = "";
    }
    if (added) {
      b.pre.insertBefore(done, b.tail);
      b.lines += added;
      // The newest lines stay; the oldest go, counted.
      while (b.lines > MAX_LINES) {
        const first = b.pre.firstChild;
        if (!first || first === b.tail) break;
        first.remove();
        b.lines--;
        b.hidden++;
      }
      if (b.hidden) {
        b.note ??= h("div", { class: "hint wb-cut" });
        if (!b.note.isConnected) b.pre.before(b.note);
        b.note.textContent = `${b.hidden.toLocaleString()} earlier lines hidden`;
      }
    }
    b.current = rest;
    const tailStyle = { ...b.style };
    b.tail.replaceChildren(...drawLine(rest, tailStyle));
  };

  const draw = (o: Output): void => {
    const detail = o.error_detail as ErrorDetail | undefined;
    if (detail) {
      block = null;
      if (!detail.chain?.length) return;
      const panel = errorPanel(detail, lastError, errors ?? { fix: () => undefined, goTo: () => false });
      if (lastError?.isConnected) lastError.replaceWith(panel);
      else element.append(panel);
      lastError = null;
      errors?.shown?.(detail);
      return;
    }
    if (o.output_type === "stream") {
      const name = String(o.name ?? "stdout");
      if (!block || block.name !== name || block.pre.parentElement !== element) block = newBlock(name);
      write(block, text(o.text));
      return;
    }
    block = null;
    if (o.output_type === "error") {
      lastError = drawError(o);
      element.append(lastError);
      return;
    }
    if (o.output_type === "execute_result" || o.output_type === "display_data") {
      if (rich >= MAX_RICH) {
        notDrawn++;
        notDrawnNote ??= h("div", { class: "hint wb-cut" });
        notDrawnNote.textContent = `${notDrawn.toLocaleString()} more outputs hidden`;
        element.append(notDrawnNote);
        return;
      }
      rich++;
      element.append(drawData((o.data as Record<string, unknown>) ?? {}, dark()));
    }
  };

  const flush = (): void => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    if (!waiting.length) return;
    const batch = waiting;
    waiting = [];
    // Consecutive pieces of the same stream are written as one.
    for (let i = 0; i < batch.length; i++) {
      let o = batch[i]!;
      if (o.output_type === "stream") {
        let joined = text(o.text);
        while (batch[i + 1]?.output_type === "stream" && batch[i + 1]!.name === o.name) joined += text(batch[++i]!.text);
        o = { ...o, text: joined };
      }
      draw(o);
    }
    grew();
  };

  const add = (incoming: Output): void => {
    if ((incoming as { clear?: boolean }).clear) {
      if ((incoming as { wait?: boolean }).wait) clearOnNext = true;
      else {
        kept = [];
        waiting = [];
        reset();
      }
      return;
    }
    if (clearOnNext) {
      clearOnNext = false;
      kept = [];
      waiting = [];
      reset();
    }
    const o = { ...incoming };
    delete o.late;
    if (o.error_detail) {
      // The panel's reading: drawn, never kept (the notebook saves Jupyter's own error record).
      waiting.push(o);
      timer ??= window.setTimeout(flush, FLUSH_MS);
      return;
    }
    keep(o);
    waiting.push(o);
    timer ??= window.setTimeout(flush, FLUSH_MS);
  };

  return {
    element,
    outputs: () => kept,
    set: (outputs) => {
      kept = [];
      waiting = [];
      clearOnNext = false;
      reset();
      for (const o of outputs) {
        keep(o);
        draw(o);
      }
    },
    add,
    notice: (message, kind = "info") => {
      flush();
      block = null;
      element.append(h("div", { class: `wb-notice ${kind}`, role: kind === "error" ? "alert" : "status" }, message));
      grew();
    },
    ask: (prompt, password, send) => {
      flush();
      block = null;
      const field = h("input", { type: password ? "password" : "text", class: "mono wb-input-box", "aria-label": prompt.trim() || "Input", autocomplete: "off", spellcheck: false }) as HTMLInputElement;
      const row = h("div", { class: "wb-input-row" }, h("span", { class: "wb-input-prompt mono" }, prompt), field);
      element.append(row);
      grew();
      setTimeout(() => field.focus(), 0);
      const done = (): void => {
        asking = null;
        field.disabled = true;
      };
      asking = { row, done };
      field.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const value = field.value;
        row.replaceChildren(h("span", { class: "wb-input-prompt mono" }, prompt), h("span", { class: "mono" }, password ? "•".repeat(Math.min(value.length, 12)) : value));
        done();
        void send(value);
      });
    },
    finish: () => {
      flush();
      if (asking) {
        asking.row.append(h("span", { class: "hint" }, " (not answered)"));
        asking.done();
      }
    },
  };
}

/** Draw saved records into `host` at once (a notebook opened). */
export function drawOutputs(host: HTMLElement, outputs: Output[], dark: boolean): void {
  const area = outputArea(() => dark);
  area.set(outputs);
  host.replaceChildren(...Array.from(area.element.childNodes));
}
