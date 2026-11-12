/**
 * catalogPage.ts: the blades and catalog page, in its own tab (plan decisions 14-18). [COMPOSITION]
 *
 * The rail (catalogRail.ts) on the left; the main area shows a table's columns with a first SELECT,
 * the blade save screen, or a welcome. Dragging gives plain text: a blade its `sag.blade(...)` line,
 * a table or column its quoted name.
 */
import { connect, onNotice, openedWith, token } from "./api";
import { type BladeKind, editBlade, showTemplatePicker } from "./bladeEditor";
import { mountRail, tableDetails, watchTheme } from "./catalogRail";
import { button, clear, h, toast } from "./dom";

watchTheme();

const detail = document.getElementById("detail") as HTMLElement;
const liveNote = document.querySelector<HTMLElement>(".cat-live");
let editing = false; // the save screen holds work not saved yet

function showWelcome(): void {
  editing = false;
  clear(
    detail,
    h("h2", {}, "Blades and the catalog"),
    h(
      "p",
      {},
      "A blade is any Python that reaches outside Python to load something: a database connection, a CSV or workbook, an API, a vault. Save it once here, then use it anywhere:",
    ),
    h("pre", { class: "code-box" }, 'import swissarmygrid as sag\n\nconn = sag.blade("Warehouse")'),
    h(
      "p",
      {},
      "A blade can add its source to the catalog: every schema, table and column with its type, searchable here and readable by your agent, so it can write SQL against the source. Or load one straight from Python:",
    ),
    h("pre", { class: "code-box" }, 'sag.load(conn.execute("SELECT * FROM INFORMATION_SCHEMA.COLUMNS").df(), name="Warehouse", dialect="sqlserver")'),
    h("div", { class: "row-actions" }, button("New blade", () => openEditor(null), { icon: "plus", class: "primary" })),
  );
}

function openEditor(name: string | null): void {
  const closed = (): void => {
    editing = false;
    rail.editorClosed();
    showWelcome();
  };
  const saved = (savedName: string): void => {
    editing = false; // saved: nothing to lose, so a click in the tree shows what was clicked again
    rail.bladeSaved(savedName);
  };
  if (name !== null) {
    editing = true;
    void editBlade(detail, name, null, "other" as BladeKind, saved, closed);
    return;
  }
  editing = false;
  void showTemplatePicker(detail, (template, kind) => {
    editing = true;
    void editBlade(detail, null, template, kind, saved, closed);
  });
}

const rail = mountRail({
  bladesHost: document.getElementById("blades") as HTMLElement,
  catalogHost: document.getElementById("catalog") as HTMLElement,
  editBlade: openEditor,
  isEditing: () => editing,
  showTable: (view) => {
    clear(detail, ...tableDetails(view));
    detail.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  },
  showSchema: (label, catalog) =>
    clear(detail, h("h2", {}, label), h("p", { class: "hint" }, `A schema in ${catalog}. Open it to see its tables and views.`)),
});

document.querySelector(".cat-top h1")?.after(
  button("Notebook", () => window.open(`/notebook#token=${encodeURIComponent(token)}`, "swissarmygrid-notebook")?.focus(), {
    icon: "code",
    class: "ghost",
    title: "The notebook, with these blades beside it (opens in its own tab)",
  }),
);
showWelcome();
void rail.loadBlades().then(() => rail.loadCatalogs(openedWith.get("catalog") ?? undefined));
connect((live) => {
  if (liveNote) liveNote.textContent = live ? "" : "Not connected to the server";
}, { catalog: "1" });
onNotice((notice) => {
  if (notice.type === "catalogs_changed") void rail.loadCatalogs();
  if (notice.type === "blades_changed") void rail.loadBlades();
});
