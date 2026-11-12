/**
 * relationshipsDialog.ts: the Relationships window: the model's keys and joins. [WIRE + EXPORT]
 *
 * Opened from the ribbon's Table row. It shows the shared models (the library: JSON files in the
 * library folders, routes_models.py) with Apply, imports a model file from disk, lists the
 * current keys and relationships, exports them as a model file (to add to a library folder), and
 * draws them as an ERD card in the active table's Analysis Sandbox. An agent sets the same model
 * through the MCP (relationships_set, relationships_apply_library, erd_draw).
 *
 * ROUTING: [LIBRARY ROW]   Apply -> applyLibrary(name)
 *          [IMPORT]        file picker -> importFile()
 *          [KEYS TABLE]    -> read-only, derived from relationships.currentModel()
 *          [JOINS TABLE]   -> read-only, derived from relationships.currentModel()
 *          [EXPORT]        -> exportModel()  (downloads <name>.json)
 *          [DRAW ERD]      -> drawErd()  (a diagram card in the Analysis Sandbox, then the Agent tab)
 */
import { api, grid } from "./api";
import { button, clear, h, icon, openDialog, toast } from "./dom";
import { showAgentView } from "./panes";
import { saveBlob } from "./profileView";
import { currentModel, loadRelationships, type RelationshipModel } from "./relationships";

interface LibraryModel {
  name: string;
  description: string;
  file: string;
  tables: string[];
  open_tables: string[];
  relationships: number;
}

export function openRelationships(): void {
  const { body, foot, close } = openDialog("Relationships", true);
  const slot = h("div");
  body.append(
    h("p", { class: "hint" }, "Each table's primary key and the joins between tables. Known joins line up a compare by themselves and show with a crown in the Key Finder. Tables are named by their dataset name, as pushed."),
    slot,
  );
  const file = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: () => void importFile() });
  foot.append(
    file,
    button("Import a model file", () => file.click(), { icon: "export", title: "Load a model JSON file from disk" }),
    button("Export", () => exportModel(), { icon: "export", title: "Download the current model as JSON (save it in a library folder to share it)" }),
    button("Draw ERD", () => void drawErd(close), { icon: "link", class: "primary", title: "Add the model as a diagram in this table's Analysis Sandbox" }),
    button("Close", close),
  );

  async function render(): Promise<void> {
    await loadRelationships();
    let library: { models: LibraryModel[]; folders: string[]; problems: string[] } = { models: [], folders: [], problems: [] };
    try {
      library = await api("/relationships/library");
    } catch (e) {
      toast((e as Error).message, "error");
    }
    const model = currentModel();
    const keys = Object.entries(model.tables).filter(([, t]) => t.primary_key?.length);
    clear(
      slot,
      // ===== LIBRARY =====
      h("h3", {}, "Shared models"),
      library.models.length
        ? h(
            "table",
            { class: "small" },
            h("thead", {}, h("tr", {}, ...["Model", "Tables open", "Joins", ""].map((t) => h("th", { scope: "col" }, t)))),
            h(
              "tbody",
              {},
              ...library.models.map((m) =>
                h(
                  "tr",
                  {},
                  h("td", {}, h("strong", {}, m.name), m.description ? h("div", { class: "hint" }, m.description) : null),
                  h("td", { class: "num" }, `${m.open_tables.length} of ${m.tables.length}`),
                  h("td", { class: "num" }, String(m.relationships)),
                  h("td", {}, button("Apply", () => void applyLibrary(m.name), { class: "primary" })),
                ),
              ),
            ),
          )
        : h("p", { class: "hint" }, "No shared models found."),
      library.problems.length ? h("p", { class: "status-error" }, `Could not read: ${library.problems.join("; ")}`) : null,
      h("p", { class: "hint" }, `Library folders: ${library.folders.join(" · ")}`),
      // ===== KEYS TABLE =====
      h("h3", {}, "Primary keys", model.name !== "current" ? h("span", { class: "hint" }, ` (${model.name})`) : null),
      keys.length
        ? h("table", { class: "small" }, h("tbody", {}, ...keys.map(([t, spec]) => h("tr", {}, h("td", {}, icon("key"), " ", t), h("td", {}, (spec.primary_key ?? []).join(", "))))))
        : h("p", { class: "hint" }, "None yet. Apply a shared model, import one, or ask the agent to set them."),
      // ===== JOINS TABLE =====
      h("h3", {}, "Relationships"),
      model.relationships.length
        ? h(
            "table",
            { class: "small" },
            h("thead", {}, h("tr", {}, ...["From", "To", "On", "Kind", "Note"].map((t) => h("th", { scope: "col" }, t)))),
            h(
              "tbody",
              {},
              ...model.relationships.map((r) =>
                h("tr", {}, h("td", {}, icon("crown"), " ", r.from), h("td", {}, r.to), h("td", {}, r.from_columns.map((c, i) => (c === r.to_columns[i] ? c : `${c} = ${r.to_columns[i]}`)).join(", ")), h("td", {}, r.kind), h("td", { class: "hint" }, r.note ?? "")),
              ),
            ),
          )
        : h("p", { class: "hint" }, "None yet."),
    );
  }

  async function applyLibrary(name: string): Promise<void> {
    try {
      await api(`/relationships/library/${encodeURIComponent(name)}`, "POST");
      toast(`Applied ${name}.`);
      await render();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  async function importFile(): Promise<void> {
    const picked = file.files?.[0];
    if (!picked) return;
    try {
      const parsed = JSON.parse(await picked.text()) as unknown;
      await api("/relationships", "POST", { model: parsed });
      toast(`Imported ${picked.name}.`);
      await render();
    } catch (e) {
      toast(`Could not import ${picked.name}: ${(e as Error).message}`, "error");
    } finally {
      file.value = "";
    }
  }

  void render();
}

function exportModel(): void {
  const model: RelationshipModel = currentModel();
  const name = model.name === "current" ? "relationships" : model.name.replace(/[^A-Za-z0-9_-]+/g, "_");
  saveBlob(new Blob([JSON.stringify({ ...model, name }, null, 2) + "\n"], { type: "application/json" }), `${name}.json`);
}

async function drawErd(close: () => void): Promise<void> {
  try {
    const erd = await api<{ mermaid: string }>("/relationships/erd");
    await api(`${grid}/cards`, "POST", { kind: "diagram", title: "Relationships", body: erd.mermaid });
    close();
    showAgentView();
    toast("The ERD is in the Analysis Sandbox.");
  } catch (e) {
    toast((e as Error).message, "error");
  }
}
