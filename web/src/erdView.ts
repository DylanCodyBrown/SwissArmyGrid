/**
 * erdView.ts: the ERD tab: the keys and joins between the open tables, as a diagram. [WIRE + EXPORT]
 *
 * The second app-level tab, after Agent. It draws the current relationship model (routes_models.py)
 * for the tables that are open: one box per table holding only its key columns, crow's-foot lines
 * for the joins (Mermaid erDiagram, drawn by the lazy diagram chunk). With no relationships for
 * the open tables it says so, and offers the ways to get some: import a model file, or apply a
 * shared model.
 *
 * ROUTING: [DIAGRAM]          -> read-only, from GET /relationships/erd (redrawn on relationships_updated)
 *          [EMPTY STATE]      -> read-only, when no relationship covers an open table
 *          [IMPORT]           file picker -> importModel()
 *          [SHARED MODELS]    Apply -> applyShared(name)
 *          [MANAGE]           -> relationshipsDialog.openRelationships()
 *          [FIND]             -> openRelationships(true): the window, its finder already running
 */
import { api } from "./api";
import { button, clear, h, toast } from "./dom";
import { contextOf, gridList, onAgentView, viewingErd } from "./panes";
import { currentModel, onRelationshipsChange } from "./relationships";
import { openRelationships } from "./relationshipsDialog";
import { effectiveTheme, onThemeChange } from "./theme";

let host: HTMLElement;
let diagramModule: Promise<typeof import("./lazy/diagram")> | null = null;
const diagrams = (): Promise<typeof import("./lazy/diagram")> => {
  diagramModule ??= import(/* external */ "/static/build/lazy/diagram.js" as string) as Promise<typeof import("./lazy/diagram")>;
  return diagramModule;
};

export function mountErd(element: HTMLElement): void {
  host = element;
  const redraw = (): void => { if (viewingErd()) void render(); };
  onAgentView(redraw);
  onRelationshipsChange(redraw);
  onThemeChange(redraw);
}

/** The open tables' dataset names (the model names tables by dataset). */
function openTables(): string[] {
  return [...new Set(gridList().map((g) => contextOf(g.id)?.dataset).filter((d): d is string => !!d))];
}

async function render(): Promise<void> {
  // Read the model as main.ts keeps it (loaded at start and on each relationships_updated).
  // Loading it here would notify onRelationshipsChange, which redraws: a loop.
  const model = currentModel();
  const open = new Set(openTables());
  const covered = [...new Set([...Object.keys(model.tables), ...model.relationships.flatMap((r) => [r.from, r.to])])].filter((t) => open.has(t));
  const file = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: () => void importModel(file) });
  const actions = h(
    "div",
    { class: "erd-actions" },
    file,
    button("Import a relationship file", () => file.click(), { icon: "export", title: "JSON file" }),
    button("Manage relationships", () => openRelationships(), { icon: "crown" }),
  );
  if (!covered.length) {
    clear(
      host,
      // ===== EMPTY STATE =====
      h(
        "div",
        { class: "erd-empty" },
        h("h1", { class: "sandbox-title" }, "Relationship diagram"),
        h("strong", {}, "No relationships yet."),
        button("Find relationships", () => openRelationships(true), { icon: "zoom", class: "primary" }),
        actions,
        await sharedModels(open),
      ),
    );
    return;
  }
  const slot = h("div", { class: "diagram erd-diagram", role: "img", "aria-label": `Relationships of ${covered.join(", ")}` });
  clear(
    host,
    h("h1", { class: "sandbox-title" }, "Relationship diagram"),
    actions,
    // ===== DIAGRAM =====
    slot,
  );
  try {
    const erd = await api<{ mermaid: string }>(`/relationships/erd?tables=${encodeURIComponent(covered.join(","))}`);
    const m = await diagrams();
    await m.drawDiagram(slot, erd.mermaid, effectiveTheme() !== "light");
  } catch (e) {
    slot.textContent = (e as Error).message;
  }
}

/** Shared models with at least one table open, each with Apply. */
async function sharedModels(open: Set<string>): Promise<HTMLElement | null> {
  try {
    const library = await api<{ models: { name: string; description: string; open_tables: string[]; tables: string[] }[] }>("/relationships/library");
    const fitting = library.models.filter((m) => m.open_tables.some((t) => open.has(t)));
    if (!fitting.length) return null;
    return h(
      "div",
      { class: "erd-shared" },
      h("h3", {}, "Shared models"),
      ...fitting.map((m) =>
        h(
          "div",
          { class: "erd-shared-row" },
          h("span", {}, h("strong", {}, m.name), ` · ${m.open_tables.length} of ${m.tables.length} tables open`),
          button("Apply", () => void applyShared(m.name), { class: "primary" }),
        ),
      ),
    );
  } catch {
    return null;
  }
}

async function applyShared(name: string): Promise<void> {
  try {
    await api(`/relationships/library/${encodeURIComponent(name)}`, "POST");
    toast(`Applied ${name}.`);
  } catch (e) {
    toast((e as Error).message, "error");
  }
}

async function importModel(file: HTMLInputElement): Promise<void> {
  const picked = file.files?.[0];
  if (!picked) return;
  try {
    await api("/relationships", "POST", { model: JSON.parse(await picked.text()) as unknown });
    toast(`Imported ${picked.name}.`);
  } catch (e) {
    toast(`Could not import ${picked.name}: ${(e as Error).message}`, "error");
  } finally {
    file.value = "";
  }
}
