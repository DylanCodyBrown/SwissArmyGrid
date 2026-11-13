/**
 * sandboxDashboards.ts: the Sandbox's saved dashboards (server: routes_cards.py, /dashboards).
 *
 *   [SAVE]     a name (an existing one adds a version): every card as drawn now, written on this click
 *   [SAVED]    each dashboard: when it was last saved, its cards, Open, History, Delete
 *   [HISTORY]  its versions, newest first, each with its cards' titles: Open this version
 *
 * Opening replaces the Sandbox. A chart or query card draws live on an open grid on its table, else
 * as it was saved until that table is pushed again.
 */
import { api } from "./api";
import { button, clear, h, openDialog, toast } from "./dom";

interface Saved {
  name: string;
  versions: number;
  saved: string;
  cards: number;
}

interface Version {
  version: number;
  saved: string;
  cards: { kind: string; title: string }[];
}

const when = (iso: string): string => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export function dashboardsDialog(cardsNow: number, redraw: () => void): void {
  const { body, foot, close } = openDialog("Dashboards", true);
  const name = h("input", { type: "text", placeholder: "weekly_sales", pattern: "[A-Za-z0-9_-]{1,64}", "aria-label": "Dashboard name", size: 24 });
  const list = h("div", { class: "dashboards" });

  const open = async (dashboard: string, version?: number): Promise<void> => {
    if (cardsNow && !window.confirm(`Opening ${dashboard} replaces the ${cardsNow} card${cardsNow === 1 ? "" : "s"} on the Sandbox now. Save them first if you want them back.`)) return;
    try {
      const r = await api<{ cards: number; live: number; as_saved: number }>(`/dashboards/${encodeURIComponent(dashboard)}/open`, "POST", version ? { version } : {});
      toast(`Opened ${dashboard}: ${r.cards} card${r.cards === 1 ? "" : "s"}${r.as_saved ? `, ${r.as_saved} as saved until their tables are open` : ""}.`);
      close();
      redraw();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const history = async (dashboard: string, row: HTMLElement): Promise<void> => {
    const shown = row.querySelector(".dashboard-history");
    if (shown) return void shown.remove();
    const { versions } = await api<{ versions: Version[] }>(`/dashboards/${encodeURIComponent(dashboard)}`);
    row.append(
      h(
        "ol",
        { class: "dashboard-history", reversed: true },
        ...[...versions].reverse().map((v) =>
          h(
            "li",
            {},
            h("strong", {}, when(v.saved)),
            h("span", { class: "hint" }, ` ${v.cards.length} card${v.cards.length === 1 ? "" : "s"}: ${v.cards.map((c) => c.title || c.kind).slice(0, 6).join(", ")}${v.cards.length > 6 ? "…" : ""}`),
            button("Open this version", () => void open(dashboard, v.version), { class: "ghost" }),
          ),
        ),
      ),
    );
  };

  const refresh = async (): Promise<void> => {
    const { dashboards } = await api<{ dashboards: Saved[] }>("/dashboards");
    if (!dashboards.length) return clear(list, h("p", { class: "hint" }, "No saved dashboards yet. Name the Sandbox above and save it."));
    clear(
      list,
      ...dashboards.map((d) => {
        const row: HTMLElement = h(
          "div",
          { class: "dashboard-row" },
          h("div", { class: "dashboard-head" },
            h("strong", {}, d.name),
            h("span", { class: "hint" }, `${d.cards} card${d.cards === 1 ? "" : "s"} · saved ${when(d.saved)}${d.versions > 1 ? ` · ${d.versions} versions` : ""}`),
            h("span", { class: "toolbar-gap" }),
            button("Open", () => void open(d.name), { icon: "expand", class: "primary" }),
            button("History", () => void history(d.name, row), { icon: "rows" }),
            button("Delete", () => {
              if (!window.confirm(`Delete ${d.name} and its ${d.versions} version${d.versions === 1 ? "" : "s"}?`)) return;
              void api(`/dashboards/${encodeURIComponent(d.name)}`, "DELETE").then(refresh, (e: Error) => toast(e.message, "error"));
            }, { icon: "trash", class: "ghost" }),
          ),
        );
        return row;
      }),
    );
  };

  const save = async (): Promise<void> => {
    try {
      const r = await api<{ version: number; cards: number }>("/dashboards", "POST", { name: name.value.trim() });
      toast(`Saved ${name.value.trim()}${r.version > 1 ? ` (version ${r.version})` : ""}: ${r.cards} card${r.cards === 1 ? "" : "s"}.`);
      await refresh();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const saveButton = button("Save", () => void save(), { icon: "export", class: "primary", title: cardsNow ? "Save every card as drawn now" : "The Sandbox is empty" });
  saveButton.disabled = !cardsNow;
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") void save(); });

  body.append(
    h("p", { class: "hint" }, "Saving keeps every card as it is drawn now, charts' points and tables' rows included, so a dashboard opens even when its tables are gone. Saving again under the same name adds a version. Nothing is saved unless you press Save."),
    h("div", { class: "analysis-controls" }, h("label", { class: "analysis-field" }, h("span", {}, "Save the Sandbox as"), name), saveButton),
    list,
  );
  foot.append(button("Close", close));
  void refresh().catch((e: Error) => clear(list, h("p", { class: "status-error" }, e.message)));
  name.focus();
}
