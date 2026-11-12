/**
 * dom.ts: tiny helpers for building the page without a framework. [SHARED]
 *
 * h("button", { class: "btn", onclick: fn }, "Run") is the whole idiom: like building a WPF
 * visual tree in code. Text is always set as text (never innerHTML), because cell values are
 * untrusted (plan section 6, rule 8); the only innerHTML is the fixed icon markup below and the
 * escaped markdown renderer.
 */

type Child = Node | string | number | null | undefined | false;
type Attrs = Record<string, unknown>;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2), value as EventListener);
    } else if (key === "class") {
      element.className = String(value);
    } else if (key === "dataset" && typeof value === "object") {
      Object.assign(element.dataset, value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(element.style, value);
    } else if (value === true) {
      element.setAttribute(key, "");
    } else {
      element.setAttribute(key, String(value));
    }
  }
  append(element, ...children);
  return element;
}

export function append(parent: Node, ...children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
}

export function clear(element: Element, ...children: Child[]): void {
  element.replaceChildren();
  append(element, ...children);
}

export function $(selector: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(selector);
  if (!found) throw new Error(`Missing element ${selector}`);
  return found;
}

// Line icons (24x24, stroke). Fixed markup, so innerHTML is safe here.
const ICONS: Record<string, string> = {
  // The Settings button: a cog (a toothed wheel around a hub).
  cog: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/>',
  // A known relationship (from the model), and a primary-key column.
  crown: '<path d="M3 8l4 4 5-7 5 7 4-4-2 11H5z"/><path d="M5 19h14"/>',
  key: '<circle cx="8" cy="14" r="4"/><path d="M11 11l9-9M16 6l3 3M14 8l2 2"/>',
  link: '<path d="M10 14a4 4 0 005.66 0l3-3a4 4 0 00-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 00-5.66 0l-3 3a4 4 0 005.66 5.66l1-1"/>',
  steps: '<path d="M4 6h16M4 12h10M4 18h6"/>',
  case: '<path d="M3 18l4-12 4 12M4.5 14h5M14 13.5a3 3 0 1 0 6 0v4.5M20 13.5a3 3 0 0 0-6 0"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1L7 17M17 7l2.1-2.1"/>',
  sql: '<ellipse cx="12" cy="5.5" rx="7" ry="2.5"/><path d="M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
  profile: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  code: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 3h6v3H9zM10 11l-2 2 2 2M14 11l2 2-2 2"/>',
  export: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  bottom: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18"/>',
  right: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M14 4v16"/>',
  max: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  restore: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  collapse: '<path d="M15 6l-6 6 6 6"/>',
  up: '<path d="M6 15l6-6 6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
  expand: '<path d="M9 6l6 6-6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  edit: '<path d="M4 20h4L20 8l-4-4L4 16v4z"/>',
  pin: '<path d="M9 4h6l-1 6 4 4H6l4-4-1-6zM12 14v6"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h8"/>',
  zoom: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5M11 8v6M8 11h6"/>',
  popout: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  play: '<path d="M7 5l12 7-12 7V5z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  agent: '<rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01"/>',
  bucket: '<path d="M3 7h5v13H3zM10 4h5v16h-5zM17 10h4v10h-4zM3 20h18"/>',
  tag: '<path d="M3 12V4h8l10 10-8 8L3 12z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  image: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 16l5-5 4 4 3-3 6 6"/>',
  back: '<path d="M10 6l-6 6 6 6M4 12h16"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  save: '<path d="M5 3h11l3 3v15H5zM8 3v5h8M8 21v-7h8v7"/>',
  check: '<path d="M4 12l5 5L20 6"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
  sortup: '<path d="M7 20V4M3 8l4-4 4 4M14 6h7M14 12h5M14 18h3"/>',
  sortdown: '<path d="M7 4v16M3 16l4 4 4-4M14 6h3M14 12h5M14 18h7"/>',
  columns: '<path d="M4 4h16v16H4zM9.5 4v16M14.5 4v16"/>',
  group: '<path d="M4 5h6v6H4zM14 5h6v6h-6zM4 15h16v4H4z"/>',
  rename: '<path d="M4 20h4l10-10-4-4L4 16zM13 7l4 4"/>',
  clean: '<path d="M4 20l7-7M14 4l6 6-7 7-6-6zM4 20h6"/>',
  exclude: '<circle cx="12" cy="12" r="8"/><path d="M6.5 6.5l11 11"/>',
  keep: '<path d="M4 7h16M7 12h10M10 17h4"/>',
  rows: '<path d="M4 5h16M4 10h16M4 15h16M4 20h16"/>',
};

export function icon(name: string): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon-svg");
  svg.innerHTML = ICONS[name] ?? "";
  return svg;
}

/** A button with an icon, visible text (or a label for icon-only buttons) and a tooltip. */
export function button(
  label: string,
  onclick: (event: MouseEvent) => void,
  options: { icon?: string; iconOnly?: boolean; class?: string; title?: string; pressed?: boolean } = {},
): HTMLButtonElement {
  const btn = h(
    "button",
    {
      type: "button",
      class: `btn ${options.iconOnly ? "icon" : ""} ${options.class ?? ""}`.trim(),
      title: options.title ?? label,
      // Always named, so a label hidden at narrow widths still reads to screen readers.
      "aria-label": label,
      "aria-pressed": options.pressed === undefined ? undefined : String(options.pressed),
      onclick,
    },
    options.icon ? icon(options.icon) : null,
    options.iconOnly ? null : h("span", { class: "btn-label" }, label),
  );
  return btn;
}

// ===== toasts =====

export function toast(message: string, kind: "info" | "error" = "info"): void {
  let host = document.querySelector<HTMLElement>(".toasts");
  if (!host) {
    host = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(host);
  }
  // The same message already showing (a stopped server fails every action alike): keep that
  // one up for longer rather than stack copies up the screen.
  const shown = [...host.children].find((t) => t.textContent === message && t.classList.contains("error") === (kind === "error"));
  const item = (shown as HTMLElement | undefined) ?? h("div", { class: `toast ${kind === "error" ? "error" : ""}` }, message);
  if (!shown) host.append(item);
  window.clearTimeout(Number(item.dataset.timer));
  item.dataset.timer = String(window.setTimeout(() => item.remove(), kind === "error" ? 9000 : 5000));
}

// ===== dialogs =====

export interface DialogParts {
  dialog: HTMLDialogElement;
  body: HTMLElement;
  foot: HTMLElement;
  close: () => void;
}

/** A modal dialog with a title, a body and a footer. Escape and the close button close it. */
export function openDialog(title: string, wide = false): DialogParts {
  const body = h("div", { class: "dialog-body" });
  const foot = h("div", { class: "dialog-foot" });
  const dialog = h("dialog", { class: wide ? "wide" : "", "aria-label": title });
  const close = (): void => {
    dialog.close();
    dialog.remove();
  };
  dialog.append(
    h("div", { class: "dialog-head" }, h("h2", {}, title), button("Close", close, { icon: "close", iconOnly: true, class: "ghost" })),
    body,
    foot,
  );
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, body, foot, close };
}

// ===== safe storage (per-viewer conveniences only; may be unavailable) =====

export function remember(key: string, value: string): void {
  try {
    localStorage.setItem(`swissarmygrid.${key}`, value);
  } catch {
    // Private windows and blocked storage: the setting just isn't remembered.
  }
}

export function recall(key: string): string | null {
  try {
    return localStorage.getItem(`swissarmygrid.${key}`);
  } catch {
    return null;
  }
}

// ===== formatting =====

const NUMERIC = /^(U?TINYINT|U?SMALLINT|U?INTEGER|U?BIGINT|U?HUGEINT|FLOAT|DOUBLE|REAL|DECIMAL)/;
export const isNumericType = (type: string): boolean => NUMERIC.test(type.toUpperCase());
export const isTextType = (type: string): boolean => type.toUpperCase() === "VARCHAR";
export const isDateType = (type: string): boolean => /^(DATE|TIMESTAMP)/.test(type.toUpperCase());
export const isBoolType = (type: string): boolean => type.toUpperCase() === "BOOLEAN";

const grouping = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
export const formatCount = (n: number): string => grouping.format(n);
