/**
 * timeSeriesDialog.ts: the Analyze menu's Time series and Forecast (Plans/analyze-menu.plan.md, phase C;
 * server: routes_timeseries.py).
 *
 *   [TIME SERIES]  a date column, a metric totalled per period (or a count of rows), optionally a split:
 *                  the trend, the season in words, what stands out, steps in the level, memory; a chart.
 *                  Show these rows (the rows of the periods that stand out); Forecast… with the same
 *                  date, metric and period
 *   [FORECAST]     the same, how many periods ahead, predictors each with its future (last value, its
 *                  own trend, or a value): the forecast with 80% and 95% ranges, and the held-back check
 */
import { forecastCards, forecastFigure, timeSeriesCards, timeSeriesFigure, type ForecastResult, type TimeSeriesResult } from "./analysisCards";
import { datasetName, registerAnalysis, resultButtons, tableName } from "./analyzeMenu";
import { api, grid } from "./api";
import { button, clear, h, isDateType, isNumericType, openDialog, toast } from "./dom";
import { selection, viewColumns } from "./gridView";
import { state } from "./state";
import { effectiveTheme } from "./theme";

const plotly = (): Promise<typeof import("./lazy/plotly")> => import(/* external */ "/static/build/lazy/plotly.js" as string) as Promise<typeof import("./lazy/plotly")>;

function field(label: string, control: HTMLElement): HTMLElement {
  return h("label", { class: "analysis-field" }, h("span", {}, label), control);
}

function options(names: [string, string][], chosen?: string): HTMLOptionElement[] {
  return names.map(([v, t]) => h("option", { value: v, selected: v === chosen }, t));
}

/** A chart in a window: drawn once the window has its width. */
function chart(figure: Record<string, unknown>): HTMLElement {
  const host = h("div", { class: "analysis-chart" });
  requestAnimationFrame(() => {
    void plotly()
      .then((m) => m.drawPlotly(host, JSON.stringify(figure), effectiveTheme() !== "light"))
      .catch((e: Error) => (host.textContent = `The chart could not be drawn: ${e.message}`));
  });
  return host;
}

interface SeriesForm {
  element: HTMLElement;
  read: () => Record<string, unknown>;
}

/** Date, metric, total and period: the fields both windows share. */
function seriesForm(given: Record<string, unknown>): SeriesForm {
  const columns = viewColumns();
  const dates = columns.filter((c) => isDateType(c.type)).map((c): [string, string] => [c.name, c.name]);
  const numbers = columns.filter((c) => isNumericType(c.type)).map((c): [string, string] => [c.name, c.name]);
  const picked = selection.targetColumns();
  const date = h("select", { "aria-label": "Date column" }, ...options(dates, String(given.date ?? picked.find((p) => dates.some(([d]) => d === p)) ?? dates[0]?.[0] ?? "")));
  const metric = h("select", { "aria-label": "Metric" }, ...options([["", "(rows: a count)"], ...numbers], String(given.metric ?? picked.find((p) => numbers.some(([n]) => n === p)) ?? numbers[0]?.[0] ?? "")));
  const total = h("select", { "aria-label": "Total" }, ...options([["sum", "Sum"], ["avg", "Average"]], String(given.total ?? "sum")));
  const period = h("select", { "aria-label": "Period" }, ...options([["day", "Day"], ["week", "Week"], ["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]], String(given.period ?? "month")));
  const sync = (): void => {
    total.disabled = !metric.value;
  };
  metric.addEventListener("change", sync);
  sync();
  return {
    element: h("div", { class: "analysis-controls" }, field("Date", date), field("Metric", metric), field("Total", total), field("Period", period)),
    read: () => ({ date: date.value, metric: metric.value || undefined, total: metric.value ? total.value : "count", period: period.value }),
  };
}

registerAnalysis("rows", "Time series…", () => void openTimeSeries());
registerAnalysis("expand", "Forecast…", () => void openForecast());

export async function openTimeSeries(given: Record<string, unknown> = {}): Promise<void> {
  const { body, foot, close } = openDialog("Time series", true);
  if (!viewColumns().some((c) => isDateType(c.type))) {
    body.append(h("p", { class: "status-error" }, "This view has no date column: a time series needs one (a date or a timestamp)."));
    foot.append(button("Close", close));
    return;
  }
  const form = seriesForm(given);
  const split = h("select", { "aria-label": "One line per" }, ...options([["", "(one line)"], ...viewColumns().filter((c) => !isNumericType(c.type) && !isDateType(c.type)).map((c): [string, string] => [c.name, c.name])]));
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const run = async (): Promise<void> => {
    const sent = { ...form.read(), split: split.value || undefined };
    clear(results, h("p", { class: "hint" }, "Reading the series…"));
    clear(actions);
    try {
      const r = await api<TimeSeriesResult>(`${grid}/time-series`, "POST", sent);
      clear(
        results,
        h("ul", { class: "analysis-verdict off ts-words" }, ...r.words.map((w) => h("li", {}, w))),
        chart(timeSeriesFigure(r)),
        r.seasonal && r.season_index.length
          ? h(
              "div",
              { class: "fit-bars" },
              h("div", { class: "fit-row head", "aria-hidden": "true" }, h("span", {}, "Season"), h("span", {}, "Against a typical " + r.period), h("span", {}), h("span", {}), h("span", {})),
              ...r.season_index.map((s) => {
                const share = r.multiplicative ? s.index - 1 : 0;
                return h(
                  "div",
                  { class: `fit-row ${Math.abs(share) >= 0.1 ? "far" : ""}` },
                  h("span", {}, s.position),
                  h("span", { class: "fit-track" }, h("span", { class: "fit-bar", style: { width: `${Math.min(100, Math.abs(share) * 200)}%` } })),
                  h("span", { class: "num" }, r.multiplicative ? `${share >= 0 ? "+" : ""}${(share * 100).toFixed(0)}%` : String(Math.round(s.index * 100) / 100)),
                  h("span", {}),
                  h("span", {}),
                );
              }),
            )
          : null,
        r.gaps.length ? h("p", { class: "hint" }, `${r.gaps.length} ${r.period}s had no rows: ${r.gaps.slice(0, 6).join(", ")}${r.gaps.length > 6 ? "…" : ""}.`) : null,
      );
      clear(
        actions,
        r.step ? button("Show these rows", () => void state.addStep(r.step!).then(close, (e: Error) => toast(e.message, "error")), { icon: "filter", title: "A step keeping the rows of the periods that stand out" }) : null,
        button("Forecast…", () => { close(); void openForecast(form.read()); }, { icon: "expand", class: "primary", title: "Forecast this series" }),
        ...resultButtons({
          analysis: "time_series",
          body: sent,
          cards: () => timeSeriesCards(r),
          rows: () => ({ name: tableName(datasetName(), "series", r.what), rows: r.points.map((p) => ({ period: p.period, value: p.value, trend: p.trend, season: p.season })) }),
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  body.append(h("div", {}, form.element, h("div", { class: "analysis-controls" }, field("One line per", split), button("Read", () => void run(), { icon: "play", class: "primary" }))), results);
  foot.append(actions, button("Close", close));
  await run();
}

export async function openForecast(given: Record<string, unknown> = {}): Promise<void> {
  const { body, foot, close } = openDialog("Forecast", true);
  if (!viewColumns().some((c) => isDateType(c.type))) {
    body.append(h("p", { class: "status-error" }, "This view has no date column: a forecast needs one (a date or a timestamp)."));
    foot.append(button("Close", close));
    return;
  }
  const form = seriesForm(given);
  const horizon = h("input", { type: "number", min: "1", max: "120", value: String(given.horizon ?? 12), size: 4, "aria-label": "Periods ahead" });
  const rows = viewColumns()
    .filter((c) => isNumericType(c.type))
    .map((c) => {
      const use = h("input", { type: "checkbox", "aria-label": `Use ${c.name}` });
      const future = h("select", { "aria-label": `${c.name} in the future` }, ...options([["last", "its last value"], ["trend", "its own trend"], ["value", "a value:"]]));
      const value = h("input", { type: "number", step: "any", size: 7, "aria-label": `${c.name} future value`, hidden: true });
      future.addEventListener("change", () => (value.hidden = future.value !== "value"));
      return { name: c.name, use, future, value, element: h("label", { class: "forecast-predictor" }, use, h("span", {}, c.name), future, value) };
    });
  const pool = h("details", { class: "cheat-sheet" }, h("summary", {}, "Predictors (optional): their averages per period, and their future"), h("div", { class: "cheat-body" }, ...rows.map((r) => r.element)));
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const run = async (): Promise<void> => {
    const sent = {
      ...form.read(),
      horizon: Number(horizon.value) || 12,
      predictors: rows.filter((r) => r.use.checked && r.name !== form.read().metric).map((r) => ({ column: r.name, future: r.future.value, value: r.future.value === "value" ? Number(r.value.value) : undefined })),
    };
    clear(results, h("p", { class: "hint" }, "Forecasting…"));
    clear(actions);
    try {
      const r = await api<ForecastResult>(`${grid}/forecast`, "POST", sent);
      clear(
        results,
        h("ul", { class: "analysis-verdict off ts-words" }, ...r.words.map((w) => h("li", {}, w))),
        chart(forecastFigure(r)),
        h(
          "table",
          { class: "small" },
          h("thead", {}, h("tr", {}, h("th", {}, r.period), h("th", { class: "num" }, "Forecast"), h("th", { class: "num" }, "80% range"), h("th", { class: "num" }, "95% range"))),
          h("tbody", {}, ...r.forecast.map((f) => h("tr", {}, h("td", {}, f.period), h("td", { class: "num" }, fmt(f.estimate)), h("td", { class: "num" }, `${fmt(f.low80)} to ${fmt(f.high80)}`), h("td", { class: "num" }, `${fmt(f.low95)} to ${fmt(f.high95)}`)))),
        ),
      );
      clear(
        actions,
        ...resultButtons({
          analysis: "forecast",
          body: sent,
          cards: () => forecastCards(r),
          rows: () => ({ name: tableName(datasetName(), "forecast", r.what), rows: r.forecast.map((f) => ({ ...f })) }),
        }),
      );
    } catch (e) {
      clear(results, h("p", { class: "status-error" }, (e as Error).message));
    }
  };
  body.append(form.element, h("div", { class: "analysis-controls" }, field("Periods ahead", horizon), button("Forecast", () => void run(), { icon: "play", class: "primary" })), pool, results);
  foot.append(actions, button("Close", close));
  await run();
}

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const fmt = (v: number): string => number.format(v);
