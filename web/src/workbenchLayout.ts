/**
 * workbenchLayout.ts: the Workbench's panes, after RStudio (the review of 2026-11-14 chose its
 * skeleton): always the same places, so the eye learns them. [WIRE]
 *
 *   ┌ side bar ┬──────────── source ────────────┬──── Variables ────┐
 *   │ (Ctrl+B) │ the notebook or script         │ what the kernel   │
 *   │          ├──────────── console ───────────┤ holds now         │
 *   │          │ the document's own console     │                   │
 *   └──────────┴────────────────────────────────┴───────────────────┘
 *                         status bar: kernel, environment, cursor
 *
 *   [SPLITTERS]  drag them, or focus one and use the arrow keys (Home and End for the ends); sizes are
 *                remembered in this browser (localStorage: a convenience, nothing on disk)
 *   [LAYOUTS]    Standard; Focus (the source alone, Ctrl+Shift+1); Wide data (the right column wider).
 *                Under 1100 pixels the right column opens over the source as a drawer
 *   [HELP]       F1 on a name: a card over the source (Escape or its close button puts it away)
 *
 * Pictures and charts stay in the run's output, each with Send to Sandbox (the owner, 2026-11-14: the
 * Sandbox is where they gather, so the Workbench has no Plots pane of its own).
 */
import { button, h } from "./dom";

interface Sizes {
  left: number; // the source and console column's width, in % of the window's body
  leftTop: number; // the source's height, in % of its column
  layout: "standard" | "focus" | "wide";
  rail: boolean; // the side bar shown
  right: boolean; // the right column shown
  console: boolean; // the console shown
}
const DEFAULTS: Sizes = { left: 70, leftTop: 62, layout: "standard", rail: true, right: true, console: true };
const KEY = "workbench-layout";

function load(): Sizes {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Sizes>) };
  } catch {
    return { ...DEFAULTS };
  }
}

export type PaneName = "source" | "console" | "variables";

export interface Layout {
  /** The source pane's host (the document goes in it). */
  source: HTMLElement;
  /** The console pane's host (the document's console goes in it). */
  console: HTMLElement;
  variables: HTMLElement;
  status: HTMLElement;
  /** Show help in a card over the source. */
  help: (title: string, text: string) => void;
  focus: (pane: PaneName) => void;
  /** F6: the next pane. */
  cycle: (by: 1 | -1) => void;
  setLayout: (layout: Sizes["layout"]) => void;
  toggle: (part: "rail" | "right" | "console", show?: boolean) => boolean;
  state: () => { layout: string; rail: boolean; right: boolean; console: boolean; narrow: boolean };
}

export function mountLayout(body: HTMLElement, rail: HTMLElement, main: HTMLElement, focusSource: () => void, focusConsole: () => void): Layout {
  const sizes = load();
  const save = (): void => {
    try {
      localStorage.setItem(KEY, JSON.stringify(sizes));
    } catch {
      /* storage blocked: the sizes last until the window closes */
    }
  };

  const source = h("div", { class: "wb-pane wb-source", id: "wb-doc", role: "region", "aria-label": "Source" });
  const consoleHost = h("div", { class: "wb-pane wb-console-pane" }); // the console inside names it
  const variables = h("div", { class: "wb-pane wb-env" }); // the Variables section inside names it

  // ===== help: a card over the source =====
  const helpTitle = h("strong", {}, "Help");
  const helpBody = h("pre", { class: "wb-help-body" });
  const helpCard = h(
    "section",
    { class: "wb-help-card", role: "dialog", "aria-label": "Help", hidden: true },
    h("div", { class: "wb-help-head" }, helpTitle, button("Close help", () => (helpCard.hidden = true), { icon: "close", iconOnly: true, class: "ghost" })),
    helpBody,
  );
  // Escape puts the card away wherever the focus is (it covers part of the source).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !helpCard.hidden) helpCard.hidden = true;
  });

  // ===== splitters =====
  const splitter = (orientation: "vertical" | "horizontal", label: string, get: () => number, set: (value: number) => void, host: () => HTMLElement): HTMLElement => {
    const el = h("div", { class: `wb-split ${orientation}`, role: "separator", tabindex: 0, "aria-orientation": orientation, "aria-label": label, "aria-valuemin": 15, "aria-valuemax": 85 });
    const apply = (value: number): void => {
      const v = Math.max(15, Math.min(85, Math.round(value * 10) / 10));
      set(v);
      el.setAttribute("aria-valuenow", String(Math.round(v)));
      draw();
    };
    el.setAttribute("aria-valuenow", String(Math.round(get())));
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      const box = host().getBoundingClientRect();
      const move = (m: PointerEvent): void => apply(orientation === "vertical" ? ((m.clientX - box.left) / box.width) * 100 : ((m.clientY - box.top) / box.height) * 100);
      const up = (): void => {
        el.classList.remove("dragging");
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        save();
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });
    el.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 10 : 2;
      const back = orientation === "vertical" ? "ArrowLeft" : "ArrowUp";
      const forth = orientation === "vertical" ? "ArrowRight" : "ArrowDown";
      if (e.key === back) apply(get() - step);
      else if (e.key === forth) apply(get() + step);
      else if (e.key === "Home") apply(15);
      else if (e.key === "End") apply(85);
      else return;
      e.preventDefault();
      save();
    });
    el.addEventListener("dblclick", () => {
      apply(orientation === "vertical" ? DEFAULTS.left : DEFAULTS.leftTop);
      save();
    });
    return el;
  };

  const left = h("div", { class: "wb-col wb-col-left" });
  const right = h("div", { class: "wb-col wb-col-right" });
  const panes = h("div", { class: "wb-panes" });
  left.append(source, splitter("horizontal", "Resize source and console", () => sizes.leftTop, (v) => (sizes.leftTop = v), () => left), consoleHost);
  right.append(variables);
  panes.append(left, splitter("vertical", "Resize source and Variables", () => sizes.left, (v) => (sizes.left = v), () => panes), right);
  body.replaceChildren(panes, helpCard);

  const status = h("footer", { class: "wb-status", role: "status", "aria-label": "Status" });
  main.append(status);

  const narrow = matchMedia("(max-width: 1100px)");
  const draw = (): void => {
    const focus = sizes.layout === "focus";
    panes.style.setProperty("--wb-left", `${sizes.layout === "wide" ? Math.min(sizes.left, 45) : sizes.left}%`);
    left.style.setProperty("--wb-top", `${sizes.leftTop}%`);
    panes.dataset.layout = sizes.layout;
    panes.classList.toggle("no-right", focus || !sizes.right);
    panes.classList.toggle("no-console", focus || !sizes.console);
    panes.classList.toggle("drawer", narrow.matches);
    rail.hidden = !sizes.rail;
  };
  narrow.addEventListener("change", () => {
    // A narrow window starts with the drawer closed (it would cover the source).
    if (narrow.matches) sizes.right = false;
    draw();
  });
  if (narrow.matches) sizes.right = false;
  draw();

  const order: PaneName[] = ["source", "console", "variables"];
  const focus = (pane: PaneName): void => {
    if (pane === "variables" && panes.classList.contains("no-right")) {
      sizes.right = true;
      if (sizes.layout === "focus") sizes.layout = "standard";
      draw();
    }
    if (pane === "console" && panes.classList.contains("no-console")) {
      sizes.console = true;
      if (sizes.layout === "focus") sizes.layout = "standard";
      draw();
    }
    if (pane === "source") focusSource();
    else if (pane === "console") focusConsole();
    else (variables.querySelector("input, button") as HTMLElement | null)?.focus();
  };
  const current = (): number => {
    const at = document.activeElement;
    if (source.contains(at)) return 0;
    if (consoleHost.contains(at)) return 1;
    if (variables.contains(at)) return 2;
    return -1;
  };

  return {
    source,
    console: consoleHost,
    variables,
    status,
    help: (title, text) => {
      helpTitle.textContent = title;
      helpBody.textContent = text || "No help";
      helpCard.hidden = false;
    },
    focus,
    cycle: (by) => {
      const visible = order.filter((p) => !(p === "variables" && panes.classList.contains("no-right")) && !(p === "console" && panes.classList.contains("no-console")));
      const at = visible.indexOf(order[current()] ?? "source");
      focus(visible[(at + by + visible.length) % visible.length]!);
    },
    setLayout: (layout) => {
      sizes.layout = layout;
      if (layout !== "focus") {
        sizes.right = true;
        sizes.console = true;
      }
      draw();
      save();
    },
    toggle: (part, show) => {
      sizes[part] = show ?? !sizes[part];
      if (sizes[part] && sizes.layout === "focus" && part !== "rail") sizes.layout = "standard";
      draw();
      save();
      return sizes[part];
    },
    state: () => ({ layout: sizes.layout, rail: sizes.rail, right: !panes.classList.contains("no-right"), console: !panes.classList.contains("no-console"), narrow: narrow.matches }),
  };
}
