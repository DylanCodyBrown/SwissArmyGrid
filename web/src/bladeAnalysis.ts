/**
 * bladeAnalysis.ts: your own analyses (analysis blades) in the Analyze menu. The server runs the blade
 * on the grid's rows as it shows them, in a child process (POST /grids/{id}/blade-analysis), and sends
 * back Sandbox cards: notes, tables, pictures, figures. Here they show in a window, with Send to Sandbox.
 */
import { ApiError, api, grid, token } from "./api";
import { button, clear, h, openDialog, toast } from "./dom";
import { selection } from "./gridView";
import { renderMarkdown } from "./markdown";
import { effectiveTheme } from "./theme";

interface Card {
  kind: string;
  title?: string;
  body?: string;
  columns?: string[];
  rows?: unknown[][];
  capped?: boolean;
}

export interface AnalysisBlade {
  name: string;
  description: string;
}

const plotly = (): Promise<typeof import("./lazy/plotly")> => import(/* external */ "/static/build/lazy/plotly.js" as string) as Promise<typeof import("./lazy/plotly")>;

/** The analysis blades, for the menu (none when the list can't be read). */
export async function analysisBlades(): Promise<AnalysisBlade[]> {
  try {
    return (await api<{ analyses: AnalysisBlade[] }>("/blades/analyses")).analyses;
  } catch {
    return [];
  }
}

export function openBladesPage(): void {
  window.open(`/catalog#token=${encodeURIComponent(token)}`, "swissarmygrid-catalog")?.focus();
}

function drawCard(card: Card): HTMLElement {
  if (card.kind === "note") return h("div", { class: "blade-result-note" }, renderMarkdown(card.body ?? ""));
  if (card.kind === "image") return h("img", { class: "card-image", src: card.body ?? "", alt: card.title || "A picture from the analysis" });
  if (card.kind === "table") {
    const columns = card.columns ?? [];
    return h(
      "div",
      { class: "analysis-table-wrap" },
      h(
        "table",
        { class: "small" },
        h("thead", {}, h("tr", {}, ...columns.map((c) => h("th", { scope: "col" }, c)))),
        h("tbody", {}, ...(card.rows ?? []).map((r) => h("tr", {}, ...r.map((v) => h("td", { class: typeof v === "number" ? "num" : "" }, v === null ? "(blank)" : String(v)))))),
      ),
      card.capped ? h("p", { class: "hint" }, "The first 100 rows.") : null,
    );
  }
  if (card.kind === "plotly") {
    const host = h("div", { class: "analysis-chart" });
    requestAnimationFrame(() => void plotly().then((m) => m.drawPlotly(host, card.body ?? "{}", effectiveTheme() !== "light")).catch((e: Error) => (host.textContent = e.message)));
    return host;
  }
  return h("pre", { class: "code-box" }, card.body ?? "");
}

/** Run an analysis blade on this grid (with the selected columns) and show what it returned. */
export async function runBladeAnalysis(blade: AnalysisBlade): Promise<void> {
  const { body, foot, close } = openDialog(blade.name, true);
  const results = h("div", { class: "analysis-results" });
  const actions = h("span", { class: "analysis-actions" });
  const columns = selection.targetColumns();
  body.append(blade.description ? h("p", { class: "hint" }, blade.description) : "", results);
  foot.append(actions, button("Close", close));
  const run = async (): Promise<void> => {
    clear(results, h("p", { class: "hint" }, `Running ${blade.name} on this grid's rows${columns.length ? ` (${columns.join(", ")})` : ""}…`));
    clear(actions);
    try {
      const answer = await api<{ title: string; cards: Card[] }>(`${grid}/blade-analysis`, "POST", { name: blade.name, columns });
      clear(results, ...answer.cards.map(drawCard));
      clear(
        actions,
        button("Send to Sandbox", () => {
          void (async () => {
            for (const [i, card] of answer.cards.entries()) await api(`${grid}/cards`, "POST", { ...card, title: card.title ?? (i === 0 ? answer.title : "") });
          })().then(() => toast(`In the Sandbox: ${answer.cards.length} card${answer.cards.length === 1 ? "" : "s"} from ${blade.name}.`), (e: Error) => toast(e.message, "error"));
        }, { icon: "image" }),
        button("Run again", () => void run(), { icon: "play" }),
        button("Edit on the blades page", openBladesPage, { icon: "edit", class: "ghost" }),
      );
    } catch (e) {
      const message = e instanceof ApiError ? e.message : (e as Error).message;
      clear(results, h("p", { class: "status-error" }, message), h("p", { class: "hint" }, "The blade runs in the grid server's own Python: a package it needs may be installed from the blades page."));
      clear(actions, button("Edit on the blades page", openBladesPage, { icon: "edit" }));
    }
  };
  await run();
}
