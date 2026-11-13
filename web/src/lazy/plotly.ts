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
function themed<T>(value: T): T {
  if (typeof value === "string") {
    const match = /^theme:([a-z0-9-]+)$/.exec(value);
    if (!match) return value;
    return (getComputedStyle(document.documentElement).getPropertyValue(`--${match[1]}`).trim() || value) as T;
  }
  if (Array.isArray(value)) return value.map(themed) as T;
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, themed(v)])) as T;
  return value;
}

export async function drawPlotly(host: HTMLElement, text: string, dark: boolean): Promise<PlotlyDrawn> {
  const checked = safeFigure(text);
  const data = themed(checked.data);
  const layout = themed(checked.layout);
  const ink = dark ? "#e8ecef" : "#1c2228";
  const grid = dark ? "#343c45" : "#c8d0d8";
  const styled = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: { l: 48, r: 16, t: layout.title ? 40 : 16, b: 40 },
    colorway: themePalette(),
    ...layout,
    font: { color: ink, ...((layout.font as object | undefined) ?? {}) },
    xaxis: { gridcolor: grid, zerolinecolor: grid, ...((layout.xaxis as object | undefined) ?? {}) },
    yaxis: { gridcolor: grid, zerolinecolor: grid, ...((layout.yaxis as object | undefined) ?? {}) },
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
