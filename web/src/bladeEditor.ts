/**
 * bladeEditor.ts: the blade save screen, its templates, and running a blade (Refresh, Test). [VIEW]
 *
 * A blade is saved Python that reaches outside Python to load something (plan decisions 14-16).
 * The save screen names it, marks its kind (SQL connection, other, vault, secret), shows what the
 * code makes and asks for (read by the server, never run), which packages it needs and which are
 * missing, its secrets (kept in the operating system's vault; typed here, never shown again), a
 * vault's secret-to-variable map, a secret blade's variable names, and an optional catalog command.
 */
import { api, ApiError } from "./api";
import { append, button, clear, h, openDialog, toast } from "./dom";
import { exportBlades } from "./bladeLibrary";

// ===== shapes the server sends (server/routes_catalog.py) =====

export type BladeKind = "sql" | "other" | "vault" | "secret" | "analysis";

export interface BladeSummary {
  name: string;
  kind: BladeKind;
  dialect: string;
  variable: string | null;
  has_catalog: boolean;
  catalog_loaded: boolean;
  template: string | null;
  secrets: Record<string, boolean>;
  vaults: string[];
  typed: Record<string, string>;
  env_names: string[];
  /** What it is for, and words to find it by (search matches the name, the description and the tags). */
  description: string;
  tags: string[];
  /** A favourite on this computer: listed first. */
  pinned: boolean;
  /** "folder": its own .blade.py file in a blade folder (`path`); "saved": kept by the app; "pack":
   * from an installed Claude Code plugin (a marketplace), read-only. */
  source: "saved" | "folder" | "pack";
  pack?: string | null; // the pack's label (sales@team-blades 1a2b3c), for a pack blade
  path: string | null;
}

interface CatalogCommand {
  language: "sql" | "python";
  command: string;
}

interface BladeDetail extends BladeSummary {
  code: string;
  catalog: CatalogCommand | null;
  vault_mapping: { env: string; secret: string }[];
  inspect?: Inspection;
}

interface Package {
  module: string;
  pip: string;
  guessed: boolean;
  installed: boolean;
}

interface Inspection {
  variable: string | null;
  assigned: string[];
  secrets: string[];
  imports: string[];
  import_lines: string[];
  packages: Package[];
  install_here: string | null;
  install_yours: string | null;
  settings?: BladeSetting[];
  error?: string;
}

/** A value in a blade's code that each person fills in for themselves (blade_core/fill.py). */
export interface BladeSetting {
  id: string; // its place in this code: changes when the code does
  name: string; // server, database, user, path...
  key: string; // as the code writes it: host, Server, dbname...
  value: string | number;
  type: "text" | "number";
  line: number;
  where: string;
  placeholder: boolean; // still a stand-in such as my-server
  suggestions: { value: string; from: string }[];
}

/** One field for a setting: its value, a "needs your value" mark on a placeholder, and the suggested
 * values as buttons (from the user's other blades, or a standard environment variable). `onValue` gets
 * every change; `commit` (optional) a finished one: Enter, leaving the field, or a suggestion. */
export function settingField(setting: BladeSetting, onValue: (value: string) => void, commit?: () => void): HTMLElement {
  const input = h("input", {
    type: setting.type === "number" ? "number" : "text",
    value: String(setting.value),
    spellcheck: "false",
    "aria-label": `${setting.key} (${setting.name})`,
    class: setting.placeholder ? "needs-value" : undefined,
  });
  input.addEventListener("input", () => onValue(input.value));
  if (commit) input.addEventListener("change", commit);
  const chips = setting.suggestions.slice(0, 4).map((s) =>
    button(s.value, () => {
      input.value = s.value;
      onValue(s.value);
      commit?.();
    }, { class: "chip suggestion", title: `Use ${s.value}: ${s.from}` }),
  );
  return h(
    "div",
    { class: "setting-row" },
    h(
      "label",
      {},
      h("span", { class: "setting-key" }, setting.key),
      h("span", { class: "hint" }, setting.name !== setting.key.toLowerCase() ? ` ${setting.name}, line ${setting.line}` : ` line ${setting.line}`),
      setting.placeholder ? h("span", { class: "needs-value-badge" }, "needs your value") : null,
    ),
    input,
    chips.length ? h("div", { class: "chips suggestions" }, h("span", { class: "hint" }, "Suggested:"), ...chips) : null,
  );
}

export interface Template {
  id: string;
  label: string;
  group: string;
  kind: BladeKind;
  dialect: string;
  code: string;
  variable: string;
  catalog: CatalogCommand | null;
  vault_mapping: { env: string; secret: string }[];
  note: string;
}

export const DIALECTS: [string, string][] = [
  ["sqlserver", "SQL Server / Azure SQL"],
  ["postgres", "PostgreSQL / Redshift"],
  ["mysql", "MySQL / MariaDB"],
  ["oracle", "Oracle"],
  ["sqlite", "SQLite"],
  ["duckdb", "DuckDB"],
  ["snowflake", "Snowflake"],
  ["trino", "Trino / Presto"],
  ["other", "Other"],
];

const KIND_LABELS: Record<BladeKind, string> = {
  sql: "SQL connection",
  other: "Other",
  vault: "Vault",
  secret: "Secret",
  analysis: "Analysis",
};

/** What a new analysis blade starts from: the rows in, markdown and a table out. */
const ANALYSIS_STARTER = `import pandas as pd


def analyze(df: pd.DataFrame, columns: list[str]) -> list:
    picked = df[columns] if columns else df
    note = f"## {len(df):,} rows\\n\\nWhat stands out in {', '.join(columns) or 'the table'}."
    return [note, picked.describe(include="all").transpose().reset_index(names="column")]
`;

/** "a, b,c" -> ["a", "b", "c"]: tags as typed, commas between. */
export function splitTags(text: string): string[] {
  return text.split(",").map((t) => t.trim()).filter(Boolean);
}

export function kindLabel(kind: BladeKind): string {
  return KIND_LABELS[kind];
}

const path = (name: string): string => `/blades/${encodeURIComponent(name)}`;

let templatesCache: Promise<Template[]> | null = null;
export function templates(): Promise<Template[]> {
  templatesCache ??= api<{ templates: Template[] }>("/blades/templates").then((r) => r.templates);
  return templatesCache;
}

/** The line a blade drops into a script: `conn = sag.blade("Warehouse")`. */
export function bladeLine(blade: { name: string; kind: BladeKind; variable: string | null }): string {
  const call = `sag.blade(${JSON.stringify(blade.name)})`;
  return blade.kind === "vault" || blade.kind === "secret" || !blade.variable ? call : `${blade.variable} = ${call}`;
}

// ===== running a blade =====

/** Ask for the values a secret blade holds, for this one run (never kept). */
function askTyped(needs: Record<string, string>, action: string): Promise<Record<string, string> | null> {
  return new Promise((resolve) => {
    const { body, foot, close, dialog } = openDialog(`Values for this ${action}`);
    const inputs = new Map<string, HTMLInputElement>();
    body.append(h("p", { class: "hint" }, "Typed for this run only, and never kept."));
    for (const [env, source] of Object.entries(needs)) {
      const input = h("input", { type: "password", autocomplete: "off", "aria-label": env });
      inputs.set(env, input);
      body.append(h("label", { class: "field" }, h("span", {}, `${env} `, h("span", { class: "hint" }, `(${source})`)), input));
    }
    let answered = false;
    const finish = (values: Record<string, string> | null): void => {
      answered = true;
      close();
      resolve(values);
    };
    dialog.addEventListener("close", () => {
      if (!answered) resolve(null);
    });
    foot.append(
      button("Cancel", () => finish(null), { class: "ghost" }),
      button("Run", () => {
        const values: Record<string, string> = {};
        for (const [env, input] of inputs) {
          if (!input.value) {
            input.focus();
            toast(`Type ${env}.`, "error");
            return;
          }
          values[env] = input.value;
        }
        finish(values);
      }, { class: "primary" }),
    );
    inputs.values().next().value?.focus();
  });
}

interface RunFailure {
  error?: string;
  missing?: string;
  install_here?: string;
  needs?: Record<string, string>;
}

function failureOf(error: unknown): RunFailure | null {
  if (!(error instanceof ApiError) || (error.status !== 409 && error.status !== 422)) return null;
  try {
    return JSON.parse(error.message) as RunFailure;
  } catch {
    return null;
  }
}

/** Run a blade on the server (Refresh: its catalog; Test: does it work). Asks for secret-blade
 * values when needed, and offers to install a missing driver. Returns the answer, or null. */
export async function runBlade<T>(name: string, action: "refresh" | "test"): Promise<T | null> {
  let env: Record<string, string> = {};
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await api<T>(`${path(name)}/${action}`, "POST", { env });
    } catch (error) {
      const failure = failureOf(error);
      if (failure?.needs) {
        const typed = await askTyped(failure.needs, action === "refresh" ? "refresh" : "test");
        if (!typed) return null;
        env = { ...env, ...typed };
        continue;
      }
      if (failure?.error) {
        showRunError(name, failure);
        return null;
      }
      toast((error as Error).message, "error");
      return null;
    }
  }
  return null;
}

function showRunError(name: string, failure: RunFailure): void {
  const { body, foot, close } = openDialog(`${name} did not run`, true);
  body.append(h("pre", { class: "code-box" }, failure.error ?? ""));
  if (failure.missing) {
    body.append(
      h("p", {}, `It needs ${failure.missing}, which is not installed in the grid server's Python (where Refresh runs).`),
      h("pre", { class: "code-box" }, failure.install_here ?? ""),
    );
    foot.append(
      button(`Install ${failure.missing}`, () => {
        close();
        void install([failure.missing ?? ""]);
      }, { class: "primary" }),
    );
  }
  foot.append(button("Close", close, { class: "ghost" }));
}

/** Install packages into SwissArmyGrid's own Python, on the user's click. */
export async function install(packages: string[]): Promise<boolean> {
  toast(`Installing ${packages.join(", ")}... this can take a minute.`);
  try {
    const result = await api<{ ok: boolean; output: string }>("/blades/install", "POST", { packages });
    toast(`Installed ${packages.join(", ")}.`);
    return result.ok;
  } catch (error) {
    let message = (error as Error).message;
    try {
      const parsed = JSON.parse(message) as { output?: string; command?: string };
      message = `${parsed.output ?? ""}\n\nRun it yourself: ${parsed.command ?? ""}`;
    } catch {
      // plain text already
    }
    const { body, foot, close } = openDialog("The install failed", true);
    body.append(h("pre", { class: "code-box" }, message));
    foot.append(button("Close", close));
    return false;
  }
}

// ===== the template picker =====

/** "New blade": templates by group, a blank one, and a secret blade. */
export async function showTemplatePicker(host: HTMLElement, onPick: (template: Template | null, kind: BladeKind) => void): Promise<void> {
  clear(host, h("h2", {}, "New blade"), h("p", { class: "hint" }, "Loading templates..."));
  let all: Template[];
  try {
    all = await templates();
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  const groups = new Map<string, Template[]>();
  for (const template of all) {
    const list = groups.get(template.group) ?? [];
    list.push(template);
    groups.set(template.group, list);
  }
  const column = (title: string, items: HTMLElement[]): HTMLElement => h("section", {}, h("h3", {}, title), h("ul", {}, ...items.map((item) => h("li", {}, item))));
  const pick = (template: Template): HTMLElement =>
    button(template.label, () => onPick(template, template.kind), { title: template.note || template.label });
  clear(
    host,
    h("h2", {}, "New blade"),
    h(
      "p",
      { class: "hint" },
      "A blade is any Python that reaches outside Python to load something: a database, a file, an API, a vault. Start from a template, or paste your own code.",
    ),
    h(
      "div",
      { class: "row-actions" },
      button("Paste my own code", () => onPick(null, "other"), { icon: "code", class: "primary" }),
      button("Secret blade (typed each session)", () => onPick(null, "secret"), { icon: "key" }),
    ),
    h(
      "div",
      { class: "template-groups" },
      ...[...groups.entries()].map(([group, list]) => column(group, list.map(pick))),
    ),
  );
}

// ===== the save screen =====

interface Draft {
  name: string;
  kind: BladeKind;
  dialect: string;
  code: string;
  variable: string;
  catalogOn: boolean;
  catalogLanguage: "sql" | "python";
  catalogCommand: string;
  mapping: { env: string; secret: string }[];
  envNames: string;
  template: string | null;
  description: string;
  tags: string;
}

function draftFrom(detail: BladeDetail | null, template: Template | null, kind: BladeKind): Draft {
  if (detail) {
    return {
      name: detail.name,
      kind: detail.kind,
      dialect: detail.dialect,
      code: detail.code,
      variable: detail.variable ?? "",
      catalogOn: detail.catalog !== null,
      catalogLanguage: detail.catalog?.language ?? "sql",
      catalogCommand: detail.catalog?.command ?? "",
      mapping: detail.vault_mapping.length ? detail.vault_mapping : [{ env: "", secret: "" }],
      envNames: detail.env_names.join("\n"),
      template: detail.template,
      description: detail.description ?? "",
      tags: (detail.tags ?? []).join(", "),
    };
  }
  return {
    name: template?.label ?? "",
    kind: template?.kind ?? kind,
    dialect: template?.dialect ?? "other",
    code: template?.code ?? "",
    variable: template?.variable ?? "",
    catalogOn: template?.catalog !== null && template?.catalog !== undefined,
    catalogLanguage: template?.catalog?.language ?? (template?.kind === "sql" ? "sql" : "python"),
    catalogCommand: template?.catalog?.command ?? "",
    mapping: template?.vault_mapping.length ? template.vault_mapping.map((m) => ({ ...m })) : [{ env: "", secret: "" }],
    envNames: "",
    template: template?.id ?? null,
    description: "",
    tags: "",
  };
}

/** Open the save screen for a saved blade (`name`), or a new one from a template. Calls `onSaved`
 * with the saved name, or `onClosed` when the user leaves without saving. */
export async function editBlade(
  host: HTMLElement,
  name: string | null,
  template: Template | null,
  kind: BladeKind,
  onSaved: (name: string) => void,
  onClosed: () => void,
): Promise<void> {
  let detail: BladeDetail | null = null;
  if (name) {
    try {
      detail = await api<BladeDetail>(path(name));
    } catch (error) {
      toast((error as Error).message, "error");
      return;
    }
  }
  const draft = draftFrom(detail, template, kind);
  const typedSecrets = new Map<string, string>();
  let inspection: Inspection | null = detail?.inspect ?? null;
  const status = h("div", { class: "status-line", role: "status", "aria-live": "polite" });
  const say = (text: string, kindOf: "" | "error" | "ok" = ""): void => {
    status.textContent = text;
    status.className = `status-line ${kindOf}`;
  };

  // ----- fields that stay put while the rest re-renders -----
  const nameInput = h("input", { type: "text", value: draft.name, maxlength: "64", "aria-label": "Blade name", spellcheck: "false" });
  nameInput.addEventListener("input", () => (draft.name = nameInput.value));
  const descriptionInput = h("textarea", { rows: "2", maxlength: "2000", "aria-label": "Description", placeholder: "What it loads, and for which app" }) as HTMLTextAreaElement;
  descriptionInput.value = draft.description;
  descriptionInput.addEventListener("input", () => (draft.description = descriptionInput.value));
  const tagsInput = h("input", { type: "text", value: draft.tags, "aria-label": "Tags", placeholder: "orders-function-app, warehouse, finance", spellcheck: "false" });
  tagsInput.addEventListener("input", () => (draft.tags = tagsInput.value));
  // Where a new blade is kept: in the app, or as a .blade.py file in one of the blade folders.
  const whereSelect = h("select", { "aria-label": "Keep it in" }, h("option", { value: "" }, "This app (on this computer)"));
  if (!name) {
    void api<{ folders: { path: string; exists: boolean }[] }>("/blades/folders").then((got) => {
      for (const folder of got.folders) if (folder.exists) whereSelect.append(h("option", { value: folder.path }, `Blade folder: ${folder.path}`));
      whereLabel.hidden = whereSelect.options.length < 2;
    }, () => undefined);
  }
  const whereLabel = h("label", { hidden: true }, h("span", {}, "Keep it in"), whereSelect);
  const codeBox = h("textarea", { class: "code-box", rows: "12", spellcheck: "false", "aria-label": "Blade code" }) as HTMLTextAreaElement;
  codeBox.value = draft.code;
  const madeHost = h("div", {});
  const settingsHost = h("fieldset", { hidden: true });
  const secretsHost = h("div", {});
  const packagesHost = h("div", {});
  const kindHost = h("div", {});
  const sections = h("div", { class: "editor" });

  let inspectTimer = 0;
  codeBox.addEventListener("input", () => {
    draft.code = codeBox.value;
    window.clearTimeout(inspectTimer);
    inspectTimer = window.setTimeout(() => void inspect(), 400);
  });
  codeBox.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.shiftKey) return;
    event.preventDefault(); // a Tab in code is four spaces, not a jump out of the box
    codeBox.setRangeText("    ", codeBox.selectionStart, codeBox.selectionEnd, "end");
    draft.code = codeBox.value;
  });

  async function inspect(): Promise<void> {
    if (!draft.code.trim()) {
      inspection = null;
      renderInspection();
      return;
    }
    try {
      inspection = await api<Inspection>("/blades/inspect", "POST", { code: draft.code, template: draft.template, name: name ?? undefined });
    } catch (error) {
      say((error as Error).message, "error");
      return;
    }
    if (inspection.variable && (!draft.variable || !inspection.assigned.includes(draft.variable))) {
      draft.variable = inspection.variable;
    }
    renderInspection();
  }

  /** The code's settings: one field each; a finished change is written into the code by the server. */
  function renderSettings(): void {
    const settings = draft.kind === "secret" ? [] : (inspection?.settings ?? []);
    settingsHost.hidden = !settings.length;
    const waiting = settings.filter((s) => s.placeholder).length;
    clear(
      settingsHost,
      h("legend", {}, "Settings"),
      h(
        "p",
        { class: "hint" },
        waiting
          ? `${waiting} still ${waiting === 1 ? "holds an example value" : "hold example values"}: put in your own. Each field changes that value in the code below.`
          : "The values this code is written with. Each field changes that value in the code below.",
      ),
      ...settings.map((setting) => {
        let typed = String(setting.value);
        return settingField(setting, (value) => (typed = value), () => {
          if (typed === String(setting.value)) return;
          void writeSetting(setting.id, typed);
        });
      }),
    );
  }

  async function writeSetting(id: string, value: string): Promise<void> {
    try {
      const answer = await api<{ code: string; settings: BladeSetting[] }>("/blades/fill", "POST", {
        code: draft.code,
        values: { [id]: value },
        template: draft.template,
        name: name ?? undefined,
      });
      draft.code = codeBox.value = answer.code;
      if (inspection) inspection.settings = answer.settings;
      renderSettings();
      say("Changed in the code.", "ok");
    } catch (error) {
      say((error as Error).message, "error");
      renderSettings();
    }
  }

  function renderInspection(): void {
    renderSettings();
    const found = inspection;
    if (found?.error) {
      clear(madeHost, h("span", { class: "warning-text" }, found.error));
      clear(secretsHost);
      clear(packagesHost);
      return;
    }
    // What the code makes: the variable sag.blade() hands back.
    const names = found?.assigned ?? [];
    const select = h("select", { "aria-label": "The variable it makes" });
    for (const n of names) select.append(h("option", { value: n, selected: n === draft.variable }, n));
    select.addEventListener("change", () => (draft.variable = select.value));
    clear(
      madeHost,
      names.length
        ? h("label", {}, h("span", {}, draft.kind === "vault" ? "The function that reads a secret" : "It makes (what sag.blade gives back)"), select)
        : h("span", { class: "hint" }, "The code makes nothing yet: end it with an assignment, such as conn = ..."),
    );
    // Secrets: typed here, kept in the operating system's vault.
    const secretNames = found?.secrets ?? [];
    clear(secretsHost);
    if (secretNames.length) {
      secretsHost.append(h("p", { class: "hint" }, "Kept in this computer's vault (Windows Credential Manager or macOS Keychain), never in the blade. An environment variable of the same name (from a vault or secret blade) works too."));
      for (const secret of secretNames) {
        const saved = detail?.secrets[secret] === true;
        const input = h("input", {
          type: "password",
          autocomplete: "new-password",
          placeholder: saved ? "saved (type to replace)" : "not saved",
          "aria-label": `secret ${secret}`,
        });
        input.value = typedSecrets.get(secret) ?? "";
        input.addEventListener("input", () => typedSecrets.set(secret, input.value));
        const forget = saved && name
          ? button("Forget", () => {
              void api(`${path(name)}/secrets`, "PUT", { [secret]: null }).then(() => {
                if (detail) detail.secrets[secret] = false;
                renderInspection();
                say(`Forgot ${secret}.`, "ok");
              }, (error: Error) => say(error.message, "error"));
            }, { class: "ghost" })
          : null;
        secretsHost.append(h("label", {}, h("span", {}, `secret("${secret}")`), h("div", { class: "row-actions" }, input, forget)));
      }
    }
    // Packages the code needs, and which are missing from SwissArmyGrid's Python.
    clear(packagesHost);
    const packages = found?.packages ?? [];
    if (packages.length) {
      const missing = packages.filter((p) => !p.installed).map((p) => p.pip);
      packagesHost.append(
        h(
          "div",
          { class: "chips" },
          ...packages.map((p) =>
            h(
              "span",
              { class: `chip ${p.installed ? "installed" : "missing"}`, title: p.guessed ? "A guess: installed under its import name" : p.module },
              `${p.pip}${p.installed ? " ✓" : " missing"}`,
            ),
          ),
        ),
        h(
          "div",
          { class: "row-actions" },
          missing.length
            ? button(`Install ${missing.join(", ")} into the server's Python`, () => {
                void install(missing).then((ok) => ok && void inspect());
              }, { class: "primary", icon: "plus" })
            : h("span", { class: "hint" }, "Everything it imports is installed where Refresh runs."),
          found?.install_yours
            ? button("Copy the install command for your Python", () => {
                void navigator.clipboard.writeText(found.install_yours ?? "").then(() => toast("Copied."));
              }, { icon: "copy", class: "ghost" })
            : null,
        ),
      );
    }
  }

  function renderKind(): void {
    clear(kindHost);
    const isCode = draft.kind !== "secret";
    codeFieldset.hidden = !isCode;
    codeHint.textContent = draft.kind === "analysis"
      ? "A function of the grid's rows: def analyze(df): ... gets them as a pandas DataFrame (add a second argument for the selected columns). Return markdown text, a table, a figure, or a list of them. It shows in the Analyze menu."
      : "Python that reaches outside to load something. Write secret(\"password\") where a password goes.";
    if (draft.kind === "analysis" && !draft.code.trim()) {
      draft.code = codeBox.value = ANALYSIS_STARTER;
      codeBox.dispatchEvent(new Event("input"));
    }
    if (draft.kind === "sql") {
      const dialect = h("select", { "aria-label": "SQL dialect" });
      for (const [value, label] of DIALECTS) dialect.append(h("option", { value, selected: value === draft.dialect }, label));
      dialect.addEventListener("change", () => (draft.dialect = dialect.value));
      kindHost.append(h("label", {}, h("span", {}, "SQL dialect (how the agent quotes names)"), dialect));
    }
    if (draft.kind === "vault") kindHost.append(mappingEditor());
    if (draft.kind === "secret") kindHost.append(secretNamesEditor());
    if (draft.kind === "sql" || draft.kind === "other") kindHost.append(catalogEditor());
  }

  function mappingEditor(): HTMLElement {
    const rows = h("div", { class: "list-editor" });
    const render = (): void => {
      clear(rows);
      draft.mapping.forEach((row, index) => {
        const env = h("input", { type: "text", value: row.env, placeholder: "WAREHOUSE_PASSWORD", "aria-label": `variable ${index + 1}`, spellcheck: "false" });
        const secret = h("input", { type: "text", value: row.secret, placeholder: "warehouse-password", "aria-label": `secret in the vault ${index + 1}`, spellcheck: "false" });
        env.addEventListener("input", () => (row.env = env.value.trim()));
        secret.addEventListener("input", () => (row.secret = secret.value.trim()));
        rows.append(
          h(
            "div",
            { class: "mapping-row" },
            env,
            h("span", { class: "hint" }, "←"),
            secret,
            button("Remove", () => {
              draft.mapping.splice(index, 1);
              if (!draft.mapping.length) draft.mapping.push({ env: "", secret: "" });
              render();
            }, { icon: "trash", iconOnly: true, class: "ghost" }),
          ),
        );
      });
    };
    render();
    return h(
      "fieldset",
      {},
      h("legend", {}, "Secrets in the vault → environment variables"),
      h("p", { class: "hint" }, "Running the vault (sag.blade, or before a blade that needs one of these) reads each secret live and sets the variable. The values are never kept."),
      h("div", { class: "mapping-row hint" }, h("span", {}, "Environment variable"), h("span", {}), h("span", {}, "Secret's name in the vault"), h("span", {})),
      rows,
      h("div", {}, button("Add a secret", () => {
        draft.mapping.push({ env: "", secret: "" });
        render();
      }, { icon: "plus", class: "ghost" })),
    );
  }

  function secretNamesEditor(): HTMLElement {
    const box = h("textarea", { class: "code-box", rows: "5", spellcheck: "false", "aria-label": "Environment variable names" }) as HTMLTextAreaElement;
    box.value = draft.envNames;
    box.addEventListener("input", () => (draft.envNames = box.value));
    return h(
      "fieldset",
      {},
      h("legend", {}, "Environment variables, one per line"),
      h("p", { class: "hint" }, "Only the names are saved. sag.blade() asks for each value this Python does not have yet, so after the kernel restarts you type them again. A Refresh that needs them asks here."),
      box,
    );
  }

  function catalogEditor(): HTMLElement {
    const on = h("input", { type: "checkbox", checked: draft.catalogOn });
    const body = h("div", { class: "editor" });
    const command = h("textarea", { class: "code-box", rows: "8", spellcheck: "false", "aria-label": "Catalog command" }) as HTMLTextAreaElement;
    command.value = draft.catalogCommand;
    command.addEventListener("input", () => (draft.catalogCommand = command.value));
    const language = h("div", { class: "segmented", role: "group", "aria-label": "Catalog command language" });
    const renderLanguage = (): void => {
      clear(
        language,
        ...(["sql", "python"] as const).map((value) =>
          button(value === "sql" ? "SQL on the connection" : "Python", () => {
            draft.catalogLanguage = value;
            renderLanguage();
          }, { pressed: draft.catalogLanguage === value }),
        ),
      );
    };
    renderLanguage();
    const starter = button("Starter query for this dialect", () => {
      void templates().then((all) => {
        const match = all.find((t) => t.kind === "sql" && t.dialect === draft.dialect && t.catalog?.language === "sql");
        if (!match?.catalog) {
          toast("No starter query for this dialect: write a SELECT that returns schema, table, column and type.", "error");
          return;
        }
        draft.catalogLanguage = "sql";
        draft.catalogCommand = command.value = match.catalog.command;
        renderLanguage();
      });
    }, { class: "ghost", icon: "sql" });
    body.append(
      h("div", { class: "row-actions" }, language, draft.kind === "sql" ? starter : null),
      h("p", { class: "hint" }, "Returns one row per column of the source: its table and column, and its schema and type. An INFORMATION_SCHEMA.COLUMNS query works as it is. Python: end with the table (a frame or a list of dicts), or set `catalog`."),
      command,
    );
    body.hidden = !draft.catalogOn;
    on.addEventListener("change", () => {
      draft.catalogOn = on.checked;
      body.hidden = !on.checked;
    });
    return h("fieldset", {}, h("legend", {}, h("label", { class: "check" }, on, "Add to the catalog")), body);
  }

  // ----- the kind switch, the code, the buttons -----
  const kinds = h("div", { class: "segmented", role: "group", "aria-label": "Kind of blade" });
  const renderKinds = (): void => {
    clear(
      kinds,
      ...(Object.keys(KIND_LABELS) as BladeKind[]).map((value) =>
        button(KIND_LABELS[value], () => {
          draft.kind = value;
          renderKinds();
          renderKind();
          renderInspection();
        }, { pressed: draft.kind === value }),
      ),
    );
  };
  const codeHint = h("p", { class: "hint" });
  const codeFieldset = h(
    "fieldset",
    {},
    h("legend", {}, "Code"),
    codeHint,
    codeBox,
    madeHost,
    secretsHost,
    packagesHost,
  );

  function body(): Record<string, unknown> {
    const out: Record<string, unknown> = {
      name: draft.name.trim(),
      kind: draft.kind,
      template: draft.template,
      description: draft.description.trim(),
      tags: splitTags(draft.tags),
    };
    if (!savedName && whereSelect.value) out.folder = whereSelect.value;
    if (draft.kind === "secret") {
      out.env_names = draft.envNames.split(/[\s,]+/).filter(Boolean);
      return out;
    }
    out.code = draft.code;
    out.variable = draft.variable;
    if (draft.kind === "sql") out.dialect = draft.dialect;
    if (draft.kind === "vault") out.vault_mapping = draft.mapping.filter((m) => m.env || m.secret);
    if ((draft.kind === "sql" || draft.kind === "other") && draft.catalogOn && draft.catalogCommand.trim()) {
      out.catalog = { language: draft.catalogLanguage, command: draft.catalogCommand };
    }
    const secrets = Object.fromEntries([...typedSecrets].filter(([, value]) => value));
    if (Object.keys(secrets).length) out.secrets = secrets;
    return out;
  }

  let savedName = name;
  // A pack's blade is read-only here: saving makes a copy of your own (named "<name> copy" unless
  // renamed first); the pack itself changes in its repository.
  let fromPack = Boolean(detail?.pack);
  async function save(then: "close" | "refresh" | "test"): Promise<void> {
    say("Saving...");
    try {
      const sent = body();
      if (fromPack && String(sent.name).toLowerCase() === (name ?? "").toLowerCase()) {
        sent.name = draft.name = nameInput.value = `${name} copy`;
      }
      const target = fromPack ? null : savedName;
      const saved = await api<BladeSummary>(target ? path(target) : "/blades", target ? "PUT" : "POST", sent);
      fromPack = false;
      savedName = saved.name;
      typedSecrets.clear();
      say(`Saved ${saved.name}.`, "ok");
      if (then === "refresh") {
        say(`Saved. Refreshing ${saved.name}'s catalog...`);
        const answer = await runBlade<{ catalog: { columns: number; tables: number } }>(saved.name, "refresh");
        if (answer) say(`Refreshed: ${answer.catalog.tables} tables, ${answer.catalog.columns} columns.`, "ok");
        else say("Saved, but the refresh did not finish.", "error");
      }
      if (then === "test") {
        say(`Saved. Testing ${saved.name}...`);
        const answer = await runBlade<{ ok: boolean; type?: string; variables?: string[] }>(saved.name, "test");
        if (answer?.variables) say(`It works: it sets ${answer.variables.join(", ")}.`, "ok");
        else if (answer?.type) say(`It works: it makes a ${answer.type}.`, "ok");
        else say("Saved, but the test did not pass.", "error");
      }
      onSaved(saved.name);
    } catch (error) {
      say((error as Error).message, "error");
    }
  }

  async function remove(): Promise<void> {
    if (!savedName) return;
    const target = savedName;
    const { body: dialogBody, foot, close } = openDialog(`Remove ${target}?`);
    dialogBody.append(h("p", {}, "The blade and its saved secrets are removed. Its catalog stays until you remove it."));
    foot.append(
      button("Cancel", close, { class: "ghost" }),
      button("Remove", () => {
        close();
        void api(path(target), "DELETE").then(() => {
          toast(`Removed ${target}.`);
          onClosed();
        }, (error: Error) => toast(error.message, "error"));
      }, { class: "danger" }),
    );
  }

  renderKinds();
  clear(
    host,
    h("h2", {}, name ? `Blade: ${name}` : "Save as blade"),
    template?.note ? h("p", { class: "hint" }, template.note) : null,
    sections,
  );
  append(
    sections,
    h("label", {}, h("span", {}, "Name"), nameInput),
    detail?.pack
      ? h(
          "p",
          { class: "warning-text" },
          `From the pack ${detail.pack}, installed from your team's marketplace. It is read-only here: Save a copy makes one of your own; the pack itself changes in its repository (a clone added as a blade folder, then a pull request).`,
        )
      : detail?.path
        ? h("p", { class: "hint" }, "Kept in its own file: ", h("code", {}, detail.path), ". Saving here writes that file.")
        : null,
    h("label", {}, h("span", {}, "Description"), descriptionInput),
    h("label", {}, h("span", {}, "Tags (commas between; search finds a blade by any of them)"), tagsInput),
    name ? null : whereLabel,
    h("div", {}, h("div", { class: "hint" }, "Kind"), kinds),
    settingsHost,
    codeFieldset,
    kindHost,
    h(
      "div",
      { class: "row-actions" },
      ...(detail?.pack
        ? [
            button("Save a copy", () => void save("close"), { class: "primary", icon: "copy", title: "Make a blade of your own from this one" }),
            button("Test", () => void runBlade(detail.name, "test").then((a) => a && say("It works.", "ok")), { icon: "check", title: "Run the pack's blade as it is" }),
            detail.catalog ? button("Refresh the catalog", () => void runBlade(detail.name, "refresh").then((a) => a && say("Refreshed.", "ok")), { icon: "play" }) : null,
          ]
        : [
            button("Save", () => void save("close"), { class: "primary", icon: "save" }),
            button("Save and refresh the catalog", () => void save("refresh"), { icon: "play" }),
            button("Save and test", () => void save("test"), { icon: "check" }),
          ]),
      button("Cancel", onClosed, { class: "ghost" }),
      name ? button("Export", () => void exportBlades(savedName ?? name), { icon: "export", class: "ghost", title: "Download it as a .blade.py file anyone can import" }) : null,
      name && !detail?.path ? button("Remove", () => void remove(), { class: "danger", icon: "trash" }) : null,
    ),
    status,
  );
  renderKind();
  if (draft.code && !inspection) await inspect();
  else renderInspection();
  nameInput.focus();
}
