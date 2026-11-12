/**
 * lazy/charts.ts: draws chart cards with Vega-Lite (loaded on first use). [WIRE]
 *
 * The server already aggregated the data and built the Vega-Lite spec (charts/spec.py); this
 * module adds the theme (colors from the CSS tokens, so all four themes and Windows high
 * contrast follow), fill patterns for series, mouse-wheel zoom for scatter and line charts,
 * and click-to-filter.
 *
 * Never color alone (plan section 13): bar and area series are filled with a pattern (diagonal,
 * cross-hatch, dots, horizontal, vertical, checks, ...) over their hue, in the bars and in the
 * legend swatches, so they stay apart in grayscale. The patterns live in one hidden <svg> in the
 * page, rebuilt when the theme changes; exported images get the same definitions inlined.
 */
import embed, { type Result } from "vega-embed";

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888888";
}

const isHc = (): boolean => (document.documentElement.dataset.theme ?? "").startsWith("hc");

// ===== fill patterns =====

const TEXTURES: ((ink: string) => string)[] = [
  (ink) => `<path d="M-2 2l4-4M0 10L10 0M8 12l4-4" stroke="${ink}" stroke-width="1.6"/>`, // diagonal
  () => "", // solid: the first series stays plain
  (ink) => `<circle cx="5" cy="5" r="1.7" fill="${ink}"/>`, // dots
  (ink) => `<path d="M0 3h10M0 8h10" stroke="${ink}" stroke-width="1.4"/>`, // horizontal
  (ink) => `<path d="M3 0v10M8 0v10" stroke="${ink}" stroke-width="1.4"/>`, // vertical
  (ink) => `<path d="M0 0L10 10M10 0L0 10" stroke="${ink}" stroke-width="1.2"/>`, // cross-hatch
  (ink) => `<rect x="0" y="0" width="5" height="5" fill="${ink}" fill-opacity="0.55"/><rect x="5" y="5" width="5" height="5" fill="${ink}" fill-opacity="0.55"/>`, // checks
  (ink) => `<path d="M0 10L10 0" stroke="${ink}" stroke-width="2.6"/>`, // wide diagonal
];

export function patternDefs(): string {
  const ink = isHc() ? token("--bg") : "rgba(255,255,255,0.75)";
  const darkInk = isHc() ? token("--bg") : "rgba(0,0,0,0.45)";
  return TEXTURES.map((texture, i) => {
    const color = token(`--cat-${i + 1}`);
    // Light hues get a dark texture and dark hues a light one, so the texture always shows.
    const hex = color.replace("#", "");
    const lum = hex.length === 6 ? (0.299 * parseInt(hex.slice(0, 2), 16) + 0.587 * parseInt(hex.slice(2, 4), 16) + 0.114 * parseInt(hex.slice(4, 6), 16)) / 255 : 0.5;
    return `<pattern id="sag-pat-${i + 1}" patternUnits="userSpaceOnUse" width="10" height="10"><rect width="10" height="10" fill="${color}"/>${texture(lum > 0.6 ? darkInk : ink)}</pattern>`;
  }).join("");
}

export function installPatterns(): void {
  let host = document.getElementById("sag-patterns");
  if (!host) {
    host = document.createElementNS("http://www.w3.org/2000/svg", "svg") as unknown as HTMLElement;
    host.id = "sag-patterns";
    host.setAttribute("aria-hidden", "true");
    host.setAttribute("style", "position:absolute;width:0;height:0;overflow:hidden");
    document.body.append(host);
  }
  host.innerHTML = `<defs>${patternDefs()}</defs>`;
}

const PATTERNS = TEXTURES.map((_, i) => `url(#sag-pat-${i + 1})`);

// ===== theme =====

function themeConfig(scale: number, narrow: boolean): Record<string, unknown> {
  const fg = token("--fg");
  const hc = isHc();
  const muted = hc ? fg : token("--muted");
  const grid = hc ? token("--fg") : token("--border");
  const font = '"Segoe UI", system-ui, sans-serif';
  const size = Math.round(12 * scale);
  return {
    background: null,
    font,
    view: { stroke: null },
    title: { color: fg, font, fontSize: Math.round(13.5 * scale), fontWeight: 600, anchor: "start" },
    axis: {
      labelColor: muted,
      titleColor: fg,
      domainColor: token("--border-strong"),
      domainWidth: hc ? 2 : 1,
      tickColor: grid,
      gridColor: grid,
      // Lighter than the data in high contrast too, but still 3:1 against the background.
      gridOpacity: hc ? 0.45 : 0.7,
      labelFont: font,
      titleFont: font,
      labelFontSize: size,
      titleFontSize: size,
      titleFontWeight: 600,
      labelSeparation: 8,
    },
    legend: {
      labelColor: fg,
      titleColor: fg,
      labelFont: font,
      titleFont: font,
      labelFontSize: size,
      titleFontSize: size,
      symbolSize: 180,
      symbolStrokeColor: fg,
      symbolStrokeWidth: 1,
      orient: narrow ? "bottom" : "right",
      direction: narrow ? "horizontal" : "vertical",
      columns: narrow ? 3 : undefined,
    },
    range: { category: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => token(`--cat-${i}`)) },
    mark: { color: token("--cat-1") },
    bar: { stroke: fg, strokeWidth: hc ? 1.5 : 0.8, strokeOpacity: hc ? 1 : 0.7 },
    rect: { stroke: token("--bg"), strokeWidth: 0.5 },
    point: { filled: true, size: 48 },
    line: { strokeWidth: hc ? 3.5 : 2.5 },
  };
}

/** Bars and areas with a series: fill from the pattern list, in the bars and the legend. */
function withPatterns(spec: Record<string, unknown>): Record<string, unknown> {
  const mark = (spec.mark as { type?: string } | undefined)?.type;
  const encoding = spec.encoding as Record<string, Record<string, unknown>> | undefined;
  if (!encoding?.color || (mark !== "bar" && mark !== "area")) return spec;
  const { strokeDash: _dash, ...rest } = encoding;
  return { ...spec, encoding: { ...rest, color: { ...encoding.color, scale: { range: PATTERNS } } } };
}

/** Angle category labels when they would not fit side by side at this width and font size
 * (the server can't know either). Labels are never dropped, so they must turn instead. */
function fitLabels(spec: Record<string, unknown>, width: number, fontSize: number): Record<string, unknown> {
  const encoding = spec.encoding as Record<string, Record<string, unknown>> | undefined;
  const x = encoding?.x;
  const values = (spec.data as { values?: Record<string, unknown>[] } | undefined)?.values ?? [];
  if (x?.type === "temporal" && width) {
    // One tick per ~90px of plot, so a narrow card labels fewer months.
    const axis = x.axis as Record<string, unknown> | undefined;
    if (axis?.format === "%b %Y") {
      // Monthly: label every month, or every 2nd or 3rd, by how much room each label has.
      const months = new Set(values.map((v) => String(v.x).slice(0, 7))).size || 12;
      const step = Math.max(1, Math.ceil((months * 70) / Math.max(120, width - 90)));
      return withYTicks({ ...spec, encoding: { ...encoding, x: { ...x, axis: { ...axis, tickCount: { interval: "month", step } } } } });
    }
    const ticks = Math.max(3, Math.min(12, Math.floor((width - 80) / 90)));
    return withYTicks({ ...spec, encoding: { ...encoding, x: { ...x, axis: { ...(x.axis as Record<string, unknown>), tickCount: ticks } } } });
  }
  if (!x || x.type !== "nominal" || !width) return withYTicks(spec);
  const labels = [...new Set(values.map((v) => String(v.x)))];
  const longest = Math.max(0, ...labels.map((l) => l.length));
  const plot = Math.max(120, width - 90);
  const fits = longest * fontSize * 0.6 + 10 <= plot / Math.max(1, labels.length);
  // Category labels already name what the axis is: its title would only repeat it.
  const base = { ...(x.axis as Record<string, unknown> | undefined), title: null };
  const axis = fits ? base : { ...base, labelAngle: -35, labelAlign: "right", labelBaseline: "top" };
  return withYTicks({ ...spec, encoding: { ...encoding, x: { ...x, axis } } });
}

/** At least three labelled values on the y axis. */
function withYTicks(spec: Record<string, unknown>): Record<string, unknown> {
  const encoding = spec.encoding as Record<string, Record<string, unknown>> | undefined;
  const y = encoding?.y;
  if (!y || y.type !== "quantitative") return spec;
  return { ...spec, encoding: { ...encoding, y: { ...y, axis: { ...(y.axis as Record<string, unknown> | undefined), tickCount: 4 } } } };
}

export interface Drawn {
  result: Result;
  toSvg: () => Promise<string>;
  toPng: () => Promise<string>;
}

export async function drawChart(
  element: HTMLElement,
  spec: Record<string, unknown>,
  options: { onSelect?: (value: unknown) => void; zoomable?: boolean; height?: number; large?: boolean; fontScale?: number } = {},
): Promise<Drawn> {
  installPatterns();
  let copy: Record<string, unknown> = withPatterns(structuredClone(spec));
  // The server names the Vega-Lite 5 schema (Altair's, for the code it exports); the page draws
  // with its own Vega-Lite, which warned in the console for every chart about the version.
  delete copy.$schema;
  if (options.height) copy.height = options.height;
  const mark = (copy.mark as { type?: string } | undefined)?.type;
  if (options.zoomable && (mark === "point" || mark === "line" || mark === "area")) {
    // Drag to pan, wheel to zoom, double-click to reset: Vega-Lite's scale binding.
    copy = { ...copy, params: [{ name: "zoom", select: "interval", bind: "scales" }] };
  }
  const narrow = element.clientWidth > 0 && element.clientWidth < 480;
  const fontSize = 12 * (options.large ? 1.3 : 1) * (options.fontScale ?? 1);
  copy = fitLabels(copy, element.clientWidth, fontSize);
  const result = await embed(element, copy as never, {
    actions: false,
    renderer: "svg",
    config: themeConfig((options.large ? 1.3 : 1) * (options.fontScale ?? 1), narrow && !options.large) as never,
    tooltip: { theme: "custom" },
  });
  if (options.onSelect) {
    result.view.addEventListener("click", (_event, item) => {
      const datum = (item as { datum?: Record<string, unknown> } | null)?.datum;
      if (datum && "x" in datum) options.onSelect?.(datum.x);
    });
  }
  // Exports carry the pattern definitions inside the SVG, so they render anywhere.
  const toSvg = async (): Promise<string> => (await result.view.toSVG()).replace(/(<svg[^>]*>)/, `$1<defs>${patternDefs()}</defs>`);
  const toPng = async (): Promise<string> => {
    const svg = await toSvg();
    const image = new Image();
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.width * 2;
    canvas.height = image.height * 2;
    const context = canvas.getContext("2d")!;
    context.fillStyle = token("--bg");
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.scale(2, 2);
    context.drawImage(image, 0, 0);
    return canvas.toDataURL("image/png");
  };
  return { result, toSvg, toPng };
}
