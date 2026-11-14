/**
 * workbenchLayout.ts: the Workbench's panes, after RStudio (the review of 2026-11-14 chose its
 * skeleton): always the same places, so the eye learns them. [WIRE]
 *
 *   ┌ side bar ┬──────────── source ────────────┬──── Variables ────┐
 *   │ (Ctrl+B) │ the notebook or script         │ what the kernel   │
 *   │          ├──────────── console ───────────┤ holds now         │
 *   │          │ the document's own console     ├─ Plots │ Help ────┤
 *   └──────────┴────────────────────────────────┴───────────────────┘
 *                         status bar: kernel, environment, cursor
 *
 *   [SPLITTERS]  drag them, or focus one and use the arrow keys (Home and End for the ends); sizes are
 *                remembered in this browser (localStorage: a convenience, nothing on disk)
 *   [LAYOUTS]    Standard; Focus (the source alone, Ctrl+Shift+1); Wide data (the right column wider).
 *                Under 1100 pixels the right column opens over the source as a drawer
 *   [PLOTS]      every picture and chart a run draws, newest first: Previous and Next, Clear
 *   [HELP]       F1 on a name (or the palette's Help on the name at the cursor)
 */
import { button, clear, h } from "./dom";
import { drawPicture, onPicture } from "./workbenchOutputs";

interface Sizes {
  left: number; // the source and console column's width, in % of the window's body
  leftTop: number; // the source's height, in % of its column
  rightTop: number; // the Variables' height, in % of the right column
  layout: "standard" | "focus" | "wide";
  rail: boolean; // the side bar shown
  right: boolean; // the right column shown
  console: boolean; // the console shown
}
const DEFAULTS: Sizes = { left: 66, leftTop: 62, rightTop: 55, layout: "standard", rail: true, right: true, console: true };
const KEY = "workbench-layout";

function load(): Sizes {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Sizes>) };
  } catch {
    return { ...DEFAULTS };
  }
}

export type PaneName = "source" | "console" | "variables" | "lower";

export interface Layout {
  /** The source pane's host (the document goes in it). */
  source: HTMLElement;
  /** The console pane's host (the document's console goes in it). */
  console: HTMLElement;
  variables: HTMLElement;
  status: HTMLElement;
  /** Show the Help tab with this text. */
  help: (title: string, text: string) => void;
  showTab: (tab: "plots" | "help") => void;
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

  // ===== the lower right: Plots and Help =====
  const plots: Record<string, unknown>[] = [];
  let shown = 0;
  const plotBody = h("div", { class: "wb-plot-body" });
  const plotHead = h("div", { class: "wb-plot-head" });
  const helpBody = h("pre", { class: "wb-help-body" }, "F1 on a name in the editor shows its help here.");
  const helpTitle = h("strong", { class: "wb-help-title" }, "Help");
  const drawPlots = (): void => {
    if (!plots.length) {
      clear(plotHead);
      clear(plotBody, h("p", { class: "hint" }, "Pictures and charts the code draws gather here, newest first."));
      return;
    }
    clear(
      plotHead,
      button("Previous plot", () => { shown = Math.min(plots.length - 1, shown + 1); drawPlots(); }, { icon: "back", iconOnly: true, class: "ghost" }),
      h("span", { class: "hint" }, `Plot ${plots.length - shown} of ${plots.length}`),
      button("Next plot", () => { shown = Math.max(0, shown - 1); drawPlots(); }, { icon: "up", iconOnly: true, class: "ghost wb-next" }),
      h("span", { class: "spacer" }),
      button("Clear plots", () => { plots.length = 0; shown = 0; drawPlots(); }, { icon: "trash", class: "ghost" }),
    );
    clear(plotBody, drawPicture(plots[plots.length - 1 - shown]!, document.documentElement.dataset.theme?.endsWith("dark") ?? false));
  };
  onPicture((data) => {
    plots.push(data);
    if (plots.length > 50) plots.shift();
    shown = 0;
    drawPlots();
  });
  drawPlots();
  const tabs = { plots: h("button", { type: "button", role: "tab", class: "wb-lower-tab", id: "wb-tab-plots" }, "Plots"), help: h("button", { type: "button", role: "tab", class: "wb-lower-tab", id: "wb-tab-help" }, "Help") };
  const panels = {
    plots: h("div", { role: "tabpanel", class: "wb-lower-panel", "aria-labelledby": "wb-tab-plots" }, plotHead, plotBody),
    help: h("div", { role: "tabpanel", class: "wb-lower-panel", "aria-labelledby": "wb-tab-help", hidden: true }, helpTitle, helpBody),
  };
  const showTab = (tab: "plots" | "help"): void => {
    for (const name of ["plots", "help"] as const) {
      tabs[name].setAttribute("aria-selected", String(name === tab));
      tabs[name].tabIndex = name === tab ? 0 : -1;
      panels[name].hidden = name !== tab;
    }
  };
  tabs.plots.addEventListener("click", () => showTab("plots"));
  tabs.help.addEventListener("click", () => showTab("help"));
  const tabList = h("div", { class: "wb-lower-tabs", role: "tablist", "aria-label": "Plots and help" }, tabs.plots, tabs.help);
  tabList.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const next = tabs.plots.getAttribute("aria-selected") === "true" ? "help" : "plots";
    showTab(next);
    tabs[next].focus();
  });
  showTab("plots");
  const lower = h("div", { class: "wb-pane wb-lower", "aria-label": "Plots and help" }, tabList, panels.plots, panels.help);

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
      apply(orientation === "vertical" ? DEFAULTS.left : label.startsWith("Source") ? DEFAULTS.leftTop : DEFAULTS.rightTop);
      save();
    });
    return el;
  };

  const left = h("div", { class: "wb-col wb-col-left" });
  const right = h("div", { class: "wb-col wb-col-right", "aria-label": "Variables, plots and help" });
  const panes = h("div", { class: "wb-panes" });
  left.append(source, splitter("horizontal", "Source and console: drag, or the Up and Down keys", () => sizes.leftTop, (v) => (sizes.leftTop = v), () => left), consoleHost);
  right.append(variables, splitter("horizontal", "Variables and plots: drag, or the Up and Down keys", () => sizes.rightTop, (v) => (sizes.rightTop = v), () => right), lower);
  panes.append(left, splitter("vertical", "Source and the right column: drag, or the Left and Right keys", () => sizes.left, (v) => (sizes.left = v), () => panes), right);
  body.replaceChildren(panes);

  const status = h("footer", { class: "wb-status", role: "status", "aria-label": "Status" });
  main.append(status);

  const narrow = matchMedia("(max-width: 1100px)");
  const draw = (): void => {
    const focus = sizes.layout === "focus";
    panes.style.setProperty("--wb-left", `${sizes.layout === "wide" ? Math.min(sizes.left, 45) : sizes.left}%`);
    left.style.setProperty("--wb-top", `${sizes.leftTop}%`);
    right.style.setProperty("--wb-top", `${sizes.rightTop}%`);
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

  const order: PaneName[] = ["source", "console", "variables", "lower"];
  const focus = (pane: PaneName): void => {
    if ((pane === "variables" || pane === "lower") && (panes.classList.contains("no-right"))) {
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
    else if (pane === "variables") (variables.querySelector("input, button") as HTMLElement | null)?.focus();
    else (lower.querySelector('[role="tab"][aria-selected="true"]') as HTMLElement | null)?.focus();
  };
  const current = (): number => {
    const at = document.activeElement;
    if (source.contains(at)) return 0;
    if (consoleHost.contains(at)) return 1;
    if (variables.contains(at)) return 2;
    if (lower.contains(at)) return 3;
    return -1;
  };

  return {
    source,
    console: consoleHost,
    variables,
    status,
    help: (title, text) => {
      helpTitle.textContent = title;
      helpBody.textContent = text || "No help for the name at the cursor.";
      if (panes.classList.contains("no-right")) {
        sizes.right = true;
        if (sizes.layout === "focus") sizes.layout = "standard";
        draw();
      }
      showTab("help");
    },
    showTab,
    focus,
    cycle: (by) => {
      const visible = order.filter((p) => !((p === "variables" || p === "lower") && panes.classList.contains("no-right")) && !(p === "console" && panes.classList.contains("no-console")));
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
