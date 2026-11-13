/**
 * lazy/echarts.ts: Apache ECharts, loaded only when an ECharts card first draws. [FUNCTION]
 *
 * A card holds an ECharts option object as JSON. JSON carries no functions, so no formatter code
 * runs. Tooltips are drawn as text on the canvas (renderMode "richText"), never as HTML, and the page
 * sends nothing anywhere: pictures, links and the free-drawing "graphic" layer are taken out, and so
 * is the toolbox's data view (it writes the data into the page as HTML).
 */
import * as echarts from "echarts";
import { themePalette } from "./svgSafe";

const OUTSIDE = /^\s*(https?:|\/\/|image:\/\/|url\(|javascript:)/i;

export interface EChartsDrawn {
  resize: () => void;
  destroy: () => void;
  /** The chart as a PNG data URL on white, at twice its size on screen (for the markdown export). */
  toPng: () => string;
}

function clean(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(clean).filter((v) => v !== undefined);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, inner] of Object.entries(value)) {
      if (key === "graphic" || key === "link" || key === "sublink" || key === "dataView") continue;
      const kept = clean(inner);
      if (kept !== undefined) out[key] = kept;
    }
    return out;
  }
  if (typeof value === "string" && OUTSIDE.test(value)) return undefined;
  return value;
}

/** The option object, checked and cleaned. Throws with a plain message. */
export function safeOption(text: string): Record<string, unknown> {
  let option: unknown;
  try {
    option = JSON.parse(text);
  } catch {
    throw new Error("The chart is not JSON: an ECharts option object.");
  }
  if (!option || typeof option !== "object" || Array.isArray(option)) throw new Error("An ECharts chart is one option object: {\"xAxis\": ..., \"series\": [...]}.");
  const cleaned = clean(option) as Record<string, unknown>;
  const tooltips = Array.isArray(cleaned.tooltip) ? cleaned.tooltip : cleaned.tooltip ? [cleaned.tooltip] : [];
  cleaned.tooltip = tooltips.length ? tooltips.map((t) => ({ ...(t as object), renderMode: "richText" })) : { renderMode: "richText" };
  return cleaned;
}

export function drawECharts(host: HTMLElement, text: string, dark: boolean): EChartsDrawn {
  const option = safeOption(text);
  const chart = echarts.init(host, dark ? "dark" : undefined, { renderer: "canvas" });
  const ink = getComputedStyle(host).getPropertyValue("--fg").trim() || undefined;
  chart.setOption({ backgroundColor: "transparent", color: themePalette(host), textStyle: { color: ink }, ...option });
  return {
    resize: () => chart.resize(),
    destroy: () => chart.dispose(),
    toPng: () => chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" }),
  };
}
