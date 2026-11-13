/**
 * relationshipsDialog.ts: the Relationships window: tangs, the finder, and the current joins. [WIRE + EXPORT]
 *
 * A **tang** is a saved set of relationships (the owner's name: the part of a blade that anchors it
 * into the handle), kept as a .tang file. Tangs are shared like blades: the blade folders (a team share
 * or a repository) are searched for .tang files, so a tang one person saves there is in a coworker's
 * list the next time this window opens.
 *
 *   TANGS     every tang found (the person's own and the shared ones): search, apply, run; import a
 *             .tang (reviewed first: what it would change), export the current joins, save them as a
 *             tang (into the person's own tangs or a shared folder)
 *   FIND      the relationship finder: how the open tables join, worked out from the data, each
 *             suggestion with its evidence; tick and accept, or reject
 *   CURRENT   the keys and joins in use, and Run: does each still hold in the open tables?
 *
 * ROUTING: [APPLY / RUN a tang]  -> POST /relationships/library/{name} / POST /relationships/check
 *          [IMPORT]              -> POST /relationships/import (review, then apply)
 *          [EXPORT]              -> GET /relationships/tang (a .tang download)
 *          [SAVE AS TANG]        -> POST /relationships/tangs
 *          [FIND]                -> POST /relationships/find; [ACCEPT] -> POST /relationships; [REJECT] -> /reject
 *          [DRAW ERD]            -> a diagram card in the Sandbox
 */
import { api, download, grid } from "./api";
import { append, button, clear, h, icon, openDialog, toast } from "./dom";
import { showAgentView } from "./panes";
import { saveBlob } from "./profileView";
import { currentModel, loadRelationships, type Join } from "./relationships";

interface LibraryTang {
  name: string;
  description: string;
  tags: string[];
  saved_at: string;
  author: string;
  file: string;
  where: "mine" | "shared" | "package" | "environment";
  tang: boolean;
  tables: string[];
  open_tables: string[];
  relationships: number;
}

interface Library {
  models: LibraryTang[];
  folders: string[];
  shared_folders: string[];
  problems: string[];
}

interface Suggestion extends Join {
  confidence: number;
  containment: number;
  coverage: number;
  orphans: number;
  reasons: string[];
  known: boolean;
}

interface RunResult extends Join {
  status: "holds" | "orphans" | "broken" | "not open";
  detail: string;
  match?: number;
  orphan_samples?: unknown[];
  actual_kind?: string;
  notes?: string[];
}

const WHERE: Record<LibraryTang["where"], string> = { mine: "Mine", shared: "Shared", package: "Built in", environment: "Folder" };
const STATUS: Record<RunResult["status"], [string, string]> = {
  holds: ["check", "Holds"],
  orphans: ["filter", "Holds, with orphans"],
  broken: ["close", "Broken"],
  "not open": ["minus", "Not open"],
};

function on(r: Join): string {
  return r.from_columns.map((c, i) => (c === r.to_columns[i] ? c : `${c} = ${r.to_columns[i]}`)).join(", ");
}

function when(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function openRelationships(): void {
  const { body, foot, close } = openDialog("Relationships and tangs", true);
  const tangsHost = h("section", { class: "rel-section", "aria-labelledby": "rel-tangs" });
  const findHost = h("section", { class: "rel-section", "aria-labelledby": "rel-find" });
  const currentHost = h("section", { class: "rel-section", "aria-labelledby": "rel-current" });
  let library: Library = { models: [], folders: [], shared_folders: [], problems: [] };
  let filter = "";
  let runs: RunResult[] | null = null;

  body.append(
    h("p", { class: "hint" }, "Each table's key and how tables join. A saved set is a tang: share one through a shared folder (the blade folders) and your coworkers see it here. Known joins line up a compare by themselves."),
    tangsHost,
    findHost,
    currentHost,
  );
  const file = h("input", { type: "file", accept: ".tang", hidden: true, onchange: () => void importFile() }) as HTMLInputElement;
  foot.append(
    file,
    button("Import a tang", () => file.click(), { icon: "import", title: "Open a .tang file: you see what it holds and changes before applying it" }),
    button("Export", () => void exportTang(), { icon: "export", title: "Download the current joins as a .tang file" }),
    button("Save as tang", () => saveDialog(), { icon: "save", title: "Save the current joins as a tang: yours, or in a shared folder for your team" }),
    button("Draw ERD", () => void drawErd(close), { icon: "link", title: "Add the joins as a diagram in the Sandbox" }),
    button("Close", close, { class: "primary" }),
  );

  // ===== TANGS =====
  async function loadLibrary(): Promise<void> {
    try {
      library = await api<Library>("/relationships/library");
    } catch (e) {
      toast((e as Error).message, "error");
    }
    renderTangs();
  }

  function renderTangs(): void {
    const text = filter.toLowerCase();
    const shown = library.models.filter((t) => !text || [t.name, t.description, t.author, ...t.tags].some((f) => f.toLowerCase().includes(text)));
    const search = h("input", { type: "search", class: "cat-search", placeholder: "Search tangs: name, description, tag or author", "aria-label": "Search tangs", value: filter });
    search.addEventListener("input", () => {
      filter = search.value;
      const at = search.selectionStart;
      renderTangs();
      const again = tangsHost.querySelector<HTMLInputElement>("input[type=search]");
      again?.focus();
      if (at !== null) again?.setSelectionRange(at, at);
    });
    clear(
      tangsHost,
      h("h3", { id: "rel-tangs" }, "Tangs"),
      library.models.length ? search : null,
      shown.length
        ? h(
            "ul",
            { class: "tang-list" },
            ...shown.map((t) =>
              h(
                "li",
                { class: "tang-item" },
                h(
                  "div",
                  { class: "tang-text" },
                  h("strong", {}, t.name),
                  h("span", { class: `badge tang-where ${t.where}`, title: t.file }, WHERE[t.where]),
                  t.description ? h("div", { class: "hint" }, t.description) : null,
                  t.tags.length ? h("div", { class: "chips" }, ...t.tags.map((g) => h("span", { class: "chip" }, g))) : null,
                  h(
                    "div",
                    { class: "hint" },
                    `${t.relationships} joins · ${t.open_tables.length} of ${t.tables.length} tables open`,
                    t.saved_at ? ` · saved ${when(t.saved_at)}` : "",
                    t.author ? ` by ${t.author}` : "",
                  ),
                ),
                h(
                  "span",
                  { class: "row-actions" },
                  button("Run", () => void run(t.name), { icon: "play", class: "ghost", title: `Check ${t.name}'s joins against the open tables` }),
                  button("Apply", () => void apply(t.name), { class: "primary", title: "Use these joins (merged into the current ones)" }),
                ),
              ),
            ),
          )
        : h("p", { class: "hint" }, library.models.length ? "No tang matches." : "No tangs yet. Find relationships below and save them as a tang, or import one."),
      library.problems.length ? h("p", { class: "status-error" }, `Could not read: ${library.problems.join("; ")}`) : null,
      h(
        "p",
        { class: "hint" },
        library.shared_folders.length
          ? `Shared folders searched: ${library.shared_folders.join(" · ")}`
          : "No shared folder yet: add one with Blade folders on the blades page, and tangs saved there reach your team.",
      ),
    );
  }

  async function apply(name: string): Promise<void> {
    try {
      const answer = await api<{ changed: string[] }>(`/relationships/library/${encodeURIComponent(name)}`, "POST");
      toast(answer.changed.length ? `Applied ${name}; it changed: ${answer.changed.join("; ")}.` : `Applied ${name}.`);
      await renderCurrent();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  async function run(tang?: string): Promise<void> {
    try {
      const answer = await api<{ tang: string; results: RunResult[] }>("/relationships/check", "POST", tang ? { tang } : {});
      runs = answer.results;
      if (tang) showRunDialog(answer.tang, answer.results);
      else await renderCurrent();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  // ===== FIND =====
  function renderFindStart(): void {
    clear(
      findHost,
      h("h3", { id: "rel-find" }, "Find relationships"),
      h("p", { class: "hint" }, "Work out how the open tables join from their data: which columns hold another table's key, how many values match, the orphans, and why. Nothing changes until you accept."),
      button("Find relationships in the open tables", () => void find(), { icon: "zoom", class: "primary" }),
    );
  }

  async function find(): Promise<void> {
    clear(findHost, h("h3", { id: "rel-find" }, "Find relationships"), h("p", { class: "hint", role: "status" }, "Looking at every open table..."));
    let found: { tables: string[]; suggestions: Suggestion[]; keys: Record<string, string[]>; seconds: number };
    try {
      found = await api("/relationships/find", "POST", {});
    } catch (e) {
      renderFindStart();
      toast((e as Error).message, "error");
      return;
    }
    const picked = new Set(found.suggestions.filter((s) => !s.known && s.confidence >= 0.8).map((_, i) => i));
    const rows = found.suggestions.map((s, i) => {
      const box = h("input", { type: "checkbox", checked: picked.has(found.suggestions.indexOf(s)) && !s.known, disabled: s.known, "aria-label": `Accept ${s.from} to ${s.to}` }) as HTMLInputElement;
      box.addEventListener("change", () => (box.checked ? picked.add(i) : picked.delete(i)));
      return h(
        "li",
        { class: `suggestion ${s.known ? "known" : ""}` },
        box,
        h(
          "div",
          { class: "tang-text" },
          h(
            "div",
            {},
            h("strong", {}, s.from),
            " → ",
            h("strong", {}, s.to),
            h("span", { class: "hint" }, ` on ${on(s)} · ${s.kind}`),
            s.active === false ? h("span", { class: "badge" }, "inactive") : null,
            s.known ? h("span", { class: "badge" }, icon("crown"), "in use") : null,
          ),
          h("div", { class: "confidence", title: `Confidence ${Math.round(s.confidence * 100)}%` }, h("span", { style: { "--share": `${Math.round(s.confidence * 100)}%` } })),
          h("details", {}, h("summary", {}, s.evidence ?? "Why"), h("ul", { class: "hint" }, ...s.reasons.map((r) => h("li", {}, r)))),
        ),
        s.known
          ? null
          : button("Reject", () => {
              void api("/relationships/reject", "POST", { from: s.from, from_columns: s.from_columns, to: s.to, to_columns: s.to_columns }).then(() => void find());
            }, { icon: "close", iconOnly: true, class: "ghost", title: "Not a real join: do not suggest it again" }),
      );
    });
    clear(
      findHost,
      h("h3", { id: "rel-find" }, "Find relationships"),
      h("p", { class: "hint" }, `${found.suggestions.length} joins across ${found.tables.length} tables (${found.seconds} s). The confident ones are ticked.`),
      found.suggestions.length ? h("ul", { class: "suggestion-list" }, ...rows) : h("p", { class: "hint" }, "No joins found between the open tables."),
      h(
        "div",
        { class: "row-actions" },
        button("Accept the ticked joins", () => void accept(found, picked), { class: "primary", icon: "check" }),
        button("Find again", () => void find(), { class: "ghost" }),
      ),
    );
  }

  async function accept(found: { suggestions: Suggestion[]; keys: Record<string, string[]> }, picked: Set<number>): Promise<void> {
    const chosen = found.suggestions.filter((_, i) => picked.has(i));
    if (!chosen.length) return toast("Tick the joins to accept first.", "error");
    const tables: Record<string, { primary_key: string[] }> = {};
    for (const s of chosen) if (found.keys[s.to]) tables[s.to] = { primary_key: found.keys[s.to]! };
    const relationships = chosen.map(({ confidence: _c, containment: _a, coverage: _v, orphans: _o, reasons: _r, known: _k, ...join }) => join);
    try {
      await api("/relationships", "POST", { model: { tables, relationships } });
      toast(`Accepted ${chosen.length} joins. Save them as a tang to keep and share them.`);
      await renderCurrent();
      await find();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  // ===== CURRENT =====
  async function renderCurrent(): Promise<void> {
    await loadRelationships();
    const model = currentModel();
    const keys = Object.entries(model.tables).filter(([, t]) => t.primary_key?.length);
    const result = (r: Join): RunResult | undefined =>
      runs?.find((x) => x.from === r.from && x.to === r.to && x.from_columns.join() === r.from_columns.join());
    clear(
      currentHost,
      h("h3", { id: "rel-current" }, "In use now", model.name !== "current" ? h("span", { class: "hint" }, ` (${model.name})`) : null),
      model.relationships.length
        ? h(
            "table",
            { class: "small rel-table" },
            h("thead", {}, h("tr", {}, ...["From", "To", "On", "Kind", "Run", "Note"].map((t) => h("th", { scope: "col" }, t)))),
            h(
              "tbody",
              {},
              ...model.relationships.map((r) => {
                const ran = result(r);
                return h(
                  "tr",
                  { class: ran ? `run-${ran.status.replace(" ", "-")}` : "" },
                  h("td", {}, icon("crown"), " ", r.from),
                  h("td", {}, r.to),
                  h("td", {}, on(r)),
                  h("td", {}, r.kind, r.active === false ? " (inactive)" : ""),
                  h("td", { title: ran?.detail ?? "Not run yet" }, ...(ran ? [icon(STATUS[ran.status][0]), ` ${STATUS[ran.status][1]}`] : ["—"])),
                  h("td", { class: "hint" }, ran && ran.status !== "holds" ? ran.detail : r.note ?? ""),
                );
              }),
            ),
          )
        : h("p", { class: "hint" }, "No joins in use yet."),
      keys.length ? h("p", { class: "hint" }, "Keys: ", ...keys.map(([t, spec], i) => [i ? " · " : "", h("strong", {}, t), ` (${(spec.primary_key ?? []).join(", ")})`]).flat()) : null,
      model.relationships.length ? h("div", { class: "row-actions" }, button("Run against the open tables", () => void run(), { icon: "play" })) : null,
    );
  }

  // ===== files =====
  async function importFile(): Promise<void> {
    const picked = file.files?.[0];
    file.value = "";
    if (!picked) return;
    const text = await picked.text();
    let review: { tang: { name: string; description: string; tags?: string[]; author?: string; relationships: Join[] }; open_tables: string[]; missing_tables: string[]; changes: string[] };
    try {
      review = await api("/relationships/import", "POST", { text });
    } catch (e) {
      toast(`${picked.name}: ${(e as Error).message}`, "error");
      return;
    }
    const t = review.tang;
    const { body: rb, foot: rf, close: rc } = openDialog(`Import ${t.name}`);
    const where = h("select", { "aria-label": "Keep it in" }, h("option", { value: "" }, "Just apply it"), h("option", { value: "mine" }, "Apply and keep it in my tangs"), ...library.shared_folders.map((f) => h("option", { value: f }, `Apply and share it: ${f}`)));
    append(
      rb,
      t.description ? h("p", {}, t.description) : null,
      t.tags?.length ? h("div", { class: "chips" }, ...t.tags.map((g) => h("span", { class: "chip" }, g))) : null,
      h("p", { class: "hint" }, `${t.relationships.length} joins${t.author ? `, saved by ${t.author}` : ""}. Tables open: ${review.open_tables.join(", ") || "none"}${review.missing_tables.length ? `; not open: ${review.missing_tables.join(", ")}` : ""}.`),
      review.changes.length ? h("div", { class: "warning-text" }, h("strong", {}, "It changes what you use now: "), h("ul", {}, ...review.changes.map((c) => h("li", {}, c)))) : h("p", { class: "hint" }, "It changes nothing you use now; it only adds."),
      h("ul", { class: "hint" }, ...t.relationships.slice(0, 30).map((r) => h("li", {}, `${r.from} → ${r.to} on ${on(r)} (${r.kind})`))),
      h("label", { class: "field" }, h("span", {}, "Then"), where),
    );
    rf.append(
      button("Cancel", rc, { class: "ghost" }),
      button("Apply", () => {
        const save = where.value !== "";
        void api<{ where?: string }>("/relationships/import", "POST", { text, apply: true, save, folder: save && where.value !== "mine" ? where.value : null }).then(async (a) => {
          rc();
          toast(`Applied ${t.name}${a.where ? ` and kept it (${a.where === "shared" ? "shared" : "yours"})` : ""}.`);
          await loadLibrary();
          await renderCurrent();
        }, (e: Error) => toast(e.message, "error"));
      }, { class: "primary", icon: "check" }),
    );
  }

  async function exportTang(): Promise<void> {
    try {
      const got = await download("/relationships/tang");
      saveBlob(got.blob, got.name);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  function saveDialog(): void {
    const model = currentModel();
    const { body: sb, foot: sf, close: sc } = openDialog("Save as tang");
    const name = h("input", { type: "text", value: model.name !== "current" ? model.name : "", maxlength: "64", "aria-label": "Tang name", placeholder: "Sales store" });
    const description = h("textarea", { rows: "2", "aria-label": "Description", placeholder: "Which tables, and what for" }) as HTMLTextAreaElement;
    description.value = model.description ?? "";
    const tags = h("input", { type: "text", value: (model.tags ?? []).join(", "), "aria-label": "Tags", placeholder: "sales, finance" });
    const author = h("input", { type: "text", value: model.author ?? "", maxlength: "100", "aria-label": "Saved by", placeholder: "optional: your name, as coworkers know you" });
    const where = h("select", { "aria-label": "Save it in" }, h("option", { value: "" }, "My tangs (on this computer)"), ...library.shared_folders.map((f) => h("option", { value: f }, `Shared: ${f}`)));
    sb.append(
      h("label", { class: "field" }, h("span", {}, "Name"), name),
      h("label", { class: "field" }, h("span", {}, "Description"), description),
      h("label", { class: "field" }, h("span", {}, "Tags (commas between)"), tags),
      h("label", { class: "field" }, h("span", {}, "Saved by"), author),
      h("label", { class: "field" }, h("span", {}, "Save it in"), where),
      h("p", { class: "hint" }, library.shared_folders.length ? "A tang in a shared folder is in your coworkers' list the next time they open this window." : "To share tangs, add a shared folder with Blade folders on the blades page."),
    );
    sf.append(
      button("Cancel", sc, { class: "ghost" }),
      button("Save", () => {
        const payload = { name: name.value.trim(), description: description.value.trim(), tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean), author: author.value.trim(), folder: where.value || null };
        void api<{ where: string; file: string }>("/relationships/tangs", "POST", payload).then(async (a) => {
          sc();
          toast(`Saved ${payload.name} ${a.where === "shared" ? "in the shared folder" : "in your tangs"}.`);
          await loadLibrary();
          await renderCurrent();
        }, (e: Error) => toast(e.message, "error"));
      }, { class: "primary", icon: "save" }),
    );
    name.focus();
  }

  void loadLibrary();
  renderFindStart();
  void renderCurrent();
}

/** A library tang's run, in a window of its own. */
function showRunDialog(name: string, results: RunResult[]): void {
  const { body, foot, close } = openDialog(`Run ${name}`, true);
  body.append(
    h(
      "table",
      { class: "small rel-run" },
      h("thead", {}, h("tr", {}, ...["Join", "Result", "Detail"].map((t) => h("th", { scope: "col" }, t)))),
      h(
        "tbody",
        {},
        ...results.map((r) =>
          h(
            "tr",
            { class: `run-${r.status.replace(" ", "-")}` },
            h("td", {}, `${r.from} → ${r.to} on ${on(r)}`),
            h("td", {}, icon(STATUS[r.status][0]), ` ${STATUS[r.status][1]}`),
            h(
              "td",
              {},
              r.detail,
              r.orphan_samples?.length ? h("div", { class: "hint" }, `Orphans, for example: ${r.orphan_samples.map((v) => JSON.stringify(v)).join(", ")}`) : null,
              ...(r.notes ?? []).map((n) => h("div", { class: "hint" }, n)),
            ),
          ),
        ),
      ),
    ),
  );
  foot.append(button("Close", close, { class: "primary" }));
}

async function drawErd(close: () => void): Promise<void> {
  try {
    const erd = await api<{ mermaid: string }>("/relationships/erd");
    await api(`${grid}/cards`, "POST", { kind: "diagram", title: "Relationships", body: erd.mermaid });
    close();
    showAgentView();
    toast("The ERD is in the Sandbox.");
  } catch (e) {
    toast((e as Error).message, "error");
  }
}
