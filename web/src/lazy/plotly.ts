/**
 * lazy/plotly.ts: Plotly figures, loaded only when a Plotly card first draws. [FUNCTION]
 *
 * A card holds a figure as Plotly JSON ({"data": [...], "layout": {...}}), what Python's
 * fig.to_json() writes. JSON carries no functions. The page sends nothing anywhere, so nothing in a
 * figure may load from outside: pictures in the layout must be inline (data:), and map traces, which
 * fetch their tiles and outlines from the internet, are not drawn.
 */
import Plotly from "plotly.js-dist-min";
import { themePalette } from "./svgSafe";

const MAP_TRACES = /^(scattergeo|choropleth|scattermapbox|choroplethmapbox|densitymapbox|scattermap|choroplethmap|densitymap)$/;

export interface PlotlyDrawn {
  resize: () => void;
  destroy: () => void;
  /** The figure as a PNG data URL, at twice its size on screen (for the markdown export). */
  toPng: () => Promise<string>;
}

/** The figure, checked: a plain object with a data list. Throws with a plain message. */
export function safeFigure(text: string): { data: Record<string, unknown>[]; layout: Record<string, unknown> } {
  let figure: unknown;
  try {
    figure = JSON.parse(text);
  } catch {
    throw new Error("The figure is not JSON (Plotly's fig.to_json() writes it).");
  }
  const data = (figure as { data?: unknown }).data;
  if (!figure || typeof figure !== "object" || !Array.isArray(data)) throw new Error('A Plotly figure is {"data": [...], "layout": {...}}.');
  const maps = data.filter((t) => MAP_TRACES.test(String((t as { type?: unknown }).type ?? "")));
  if (maps.length) throw new Error("Map traces load their maps from the internet, which SwissArmyGrid never does; draw it in Python and add it as an image.");
  const layout = { ...((figure as { layout?: Record<string, unknown> }).layout ?? {}) };
  if (Array.isArray(layout.images)) {
    layout.images = (layout.images as { source?: unknown }[]).filter((i) => typeof i.source === "string" && i.source.startsWith("data:image/"));
  }
  delete layout.mapbox;
  delete layout.map;
  delete layout.geo;
  return { data: data as Record<string, unknown>[], layout };
}

/** A figure may name the theme's colors ("theme:accent", "theme:muted", "theme:ok"): resolved now, so
 * the same figure looks right in every theme and follows a theme change when it redraws. */
function themed<T>(value: T, where: Element): T {
  if (typeof value === "string") {
    // "theme:accent", or "theme:accent/0.2" for the theme's colour seen through (a forecast's band).
    const match = /^theme:([a-z0-9-]+)(?:\/(0?\.\d+|1))?$/.exec(value);
    if (!match) return value;
    const color = getComputedStyle(where).getPropertyValue(`--${match[1]}`).trim();
    if (!color) return value;
    if (!match[2] || !/^#[0-9a-f]{6}$/i.test(color)) return color as T;
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(color.slice(i, i + 2), 16));
    return `rgba(${r}, ${g}, ${b}, ${match[2]})` as T;
  }
  if (Array.isArray(value)) return value.map((v) => themed(v, where)) as T;
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, themed(v, where)])) as T;
  return value;
}

/** `over` laid on `under`, objects inside merged too (a figure's own axis title keeps our font). */
function merged(under: Record<string, unknown>, over: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...under };
  for (const [key, value] of Object.entries(over)) {
    const base = out[key];
    out[key] = value && typeof value === "object" && !Array.isArray(value) && base && typeof base === "object" && !Array.isArray(base)
      ? merged(base as Record<string, unknown>, value as Record<string, unknown>)
      : value;
  }
  return out;
}

export async function drawPlotly(host: HTMLElement, text: string, dark: boolean): Promise<PlotlyDrawn> {
  const checked = safeFigure(text);
  const data = themed(checked.data, host);
  const layout = themed(checked.layout, host);
  // The ink and grid lines of the theme the figure draws in (the Sandbox's own, there).
  const style = getComputedStyle(host);
  const read = (name: string, fallback: string): string => style.getPropertyValue(name).trim() || fallback;
  const ink = read("--fg", dark ? "#e8ecef" : "#1c2228");
  const muted = read("--muted", dark ? "#a2acb6" : "#525c66");
  const grid = read("--border", dark ? "#343c45" : "#c8d0d8");
  const rule = read("--border-strong", dark ? "#707b86" : "#7d8893");
  // What a chart's furniture looks like unless the figure says otherwise: a thin axis line, small ticks
  // outward, quiet tick labels with thousands separated, light dotted grid lines, the axis title in the
  // muted ink, room made for long labels. A figure's own settings win, key by key.
  const axisDefaults = {
    showline: true,
    linecolor: rule,
    linewidth: 1,
    ticks: "outside",
    ticklen: 4,
    tickcolor: rule,
    tickfont: { color: muted, size: 11 },
    gridcolor: grid,
    griddash: "dot",
    zerolinecolor: rule,
    separatethousands: true,
    automargin: true,
    title: { font: { color: muted, size: 12 }, standoff: 10 },
  };
  const axes = Object.fromEntries(
    ["xaxis", "yaxis", ...Object.keys(layout).filter((k) => /^[xy]axis\d+$/.test(k))].map((key) => [key, merged(axisDefaults, (layout[key] as Record<string, unknown> | undefined) ?? {})]),
  );
  const styled = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: { l: 8, r: 12, t: layout.title ? 44 : 32, b: 8 },
    colorway: themePalette(host),
    hoverlabel: { bgcolor: read("--raised", dark ? "#21272d" : "#ffffff"), bordercolor: rule, font: { color: ink, size: 12 } },
    ...layout,
    legend: merged({ orientation: "h", x: 0, xanchor: "left", y: 1.01, yanchor: "bottom", bgcolor: "rgba(0,0,0,0)", font: { color: ink, size: 12 }, itemsizing: "constant" }, (layout.legend as Record<string, unknown> | undefined) ?? {}),
    font: merged({ color: ink, family: '"Segoe UI", system-ui, sans-serif', size: 12 }, (layout.font as Record<string, unknown> | undefined) ?? {}),
    ...axes,
    autosize: true,
    width: undefined,
    height: undefined,
  };
  await Plotly.newPlot(host, data as never, styled as never, {
    displaylogo: false,
    responsive: false, // the card's own resize observer calls resize()
    showLink: false,
    showSendToCloud: false,
    topojsonURL: "/static/none/", // never the internet (map traces are refused above anyway)
  } as never);
  return {
    resize: () => void Plotly.Plots.resize(host),
    destroy: () => Plotly.purge(host),
    toPng: () => Plotly.toImage(host, { format: "png", width: host.clientWidth || 700, height: host.clientHeight || 420, scale: 2 } as never),
  };
}
