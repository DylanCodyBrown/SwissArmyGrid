/**
 * connectionsWindow.ts: the Application row's Connections window (Plans/workbench-and-connections.plan.md):
 * the user's connections, each a database, a file or a key vault, and adding one with a form per type.
 * [WIRE + EXPORT]
 *
 *   [LIST]   each connection: its type, its catalog, Edit, Test, Refresh catalog, Copy for Python, Remove;
 *            a key vault shows its entries (variable <- secret in the vault) instead
 *   [ADD]    pick a type (Databases, Files, Vaults), then its form:
 *   [FORM]   a name; Sign in, where the type has several ways (SQL Server, Azure SQL: Windows, user name
 *            and password, MFA), each its own code, switched keeping what was typed; the starter's
 *            settings as fields (on/off ones as checkboxes; a file or folder field has Browse…); its
 *            passwords (kept in the system keyring); Test connection (runs the form, nothing saved);
 *            Save. The Python it makes shows underneath, editable for the unusual case
 *   [VAULT]  a vault's form lists its secrets by name (never a value) as soon as it can be read (its
 *            address filled in, or opened to edit). Each becomes an environment variable: left empty,
 *            under the vault's own name (shown faintly); typed, under that name. Every secret (new ones
 *            too), or untick it and choose. Every Workbench kernel starts with them set
 *
 * ROUTING: [LIST]  GET /connections; Remove -> DELETE /blades/{name}; Refresh -> POST /blades/{name}/refresh
 *          [TYPES] GET /connections/types        [EDIT] GET /connections/{name}
 *          [FIELD] POST /blades/fill (the value into the code)   [CODE] POST /blades/inspect (its fields again)
 *          [TEST]  POST /connections/test        [SAVE] POST /connections, PUT /connections/{name}
 *          [VAULT] POST /connections/vaults/secrets (the names in it); GET /connections/vaults (a password's)
 */
import { api, onNotice } from "./api";
import { button, clear, h, openDialog, toast } from "./dom";
import { chooseFile } from "./fileChooser";

interface Field {
  id: string;
  name: string;
  key: string;
  value: string | number | boolean;
  type: "text" | "number" | "flag";
  placeholder: boolean;
  picker: "file" | "folder" | null;
  suggestions: { value: string; from: string }[];
}

interface Form {
  id: string;
  label: string;
  group: string;
  connection: "database" | "file" | "vault";
  kind: string;
  dialect: string;
  variable: string;
  catalog: { language: string; command: string } | null;
  note: string;
  code: string;
  fields: Field[];
  secrets: string[];
  install_here: string | null;
  name?: string;
  description?: string;
  summary?: { secrets: Record<string, boolean> };
  vault_mapping: Mapping[];
  vault_all?: boolean;
  /** Ways to sign in, each its own code; `mark` is text only that code holds. */
  sign_ins?: { label: string; mark: string; code: string }[];
}

interface Mapping {
  env: string;
  secret: string;
}

interface Vault {
  name: string;
  entries: Mapping[];
  /** Every secret it holds, each under its own name. */
  every: boolean;
  read_only: boolean;
}

/** After a vault saved from a password's "New vault": back to that connection's form. */
type Then = (saved: string | null) => void;

interface Connection {
  name: string;
  connection: "database" | "file" | "vault";
  /** A key vault's: each variable it sets, from which secret; `every`: every secret it holds. */
  entries?: Mapping[];
  every?: boolean;
  type: string;
  dialect: string;
  has_catalog: boolean;
  /** Its catalog loading in the background: loading, waiting (first use), failed and why; none: loaded or not asked. */
  catalog_state: { state: "loading" | "waiting" | "failed"; error?: string } | null;
  catalog_loaded: boolean;
  description: string;
  source: string;
  pack: string | null;
}

const FILE_KINDS: Record<string, string[]> = {
  csv: [".csv", ".tsv", ".txt"],
  excel: [".xlsx", ".xlsm", ".xls"],
  parquet: [".parquet"],
  json: [".json", ".jsonl", ".ndjson"],
  sqlite: [".db", ".sqlite", ".sqlite3"],
  duckdb: [".duckdb", ".db"],
  access: [".accdb", ".mdb"],
};

const words = (name: string): string => (name.charAt(0).toUpperCase() + name.slice(1)).replace(/_/g, " ");
/** A starter's label as a type's name: "A CSV file" -> "CSV file". */
const plain = (label: string): string => {
  const bare = label.replace(/^(an?|the) /i, "");
  return bare.charAt(0).toUpperCase() + bare.slice(1);
};
/** A path's file or folder name without its kind: C:\data\orders.csv -> orders. */
const stem = (path: string): string => (path.split(/[\\/]/).filter(Boolean).pop() ?? "").replace(/\.[^.]+$/, "");
/** A secret's variable, named as in the vault (domain/blades.py env_name_for_secret): SqlPassword stays,
 *  Sql--Password -> Sql__Password, warehouse-password -> warehouse_password. */
const envFor = (secret: string): string => {
  const w = secret.trim().replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").replace(/--/g, "__").replace(/[^A-Za-z0-9_]/g, "_").replace(/^_+|_+$/g, "").slice(0, 128) || "SECRET";
  return (/^\d/.test(w) ? `_${w}` : w).slice(0, 128);
};
/** What a Test answers: a vault reads its variables; a connection makes something (a file: is there). */
const worked = (r: { type?: string; found?: string; variables?: string[] }): string => (r.variables ? `read ${r.variables.join(", ")}` : (r.found ?? `made a ${r.type}`));

// The list, while it shows: drawn again as catalogs load in the background (the notices below).
let listShowing: (() => void) | null = null;
onNotice((notice) => {
  if (notice.type === "catalog_loading" || notice.type === "catalogs_changed") listShowing?.();
});

/** What a connection's catalog is doing, in a word or two (the reason on hover). */
function catalogWord(c: Connection): HTMLElement | null {
  const state = c.catalog_state?.state;
  if (state === "loading") return h("span", { class: "hint" }, "loading catalog…");
  if (state === "failed") return h("span", { class: "status-error", title: c.catalog_state?.error ?? "" }, "catalog not loaded");
  if (c.catalog_loaded) return h("span", { class: "hint" }, "catalog loaded");
  return null;
}

/** The Application row's button. */
export function connectionsButton(): HTMLButtonElement {
  return button("Connections", () => void openConnections(), { icon: "link", class: "ribbon-btn" });
}

export async function openConnections(): Promise<void> {
  const { body, foot, close } = openDialog("Connections", true);
  let drawing = 0;
  const showList = async (): Promise<void> => {
    listShowing = () => (body.isConnected ? void showList() : (listShowing = null));
    // Drawn again as catalogs load: only the newest draw paints (never twice, never over a form opened
    // meanwhile), and "Loading…" shows only the first time (no flicker).
    const mine = ++drawing;
    if (!body.querySelector(".connection-list, .empty-note")) {
      clear(foot);
      clear(body, h("p", { class: "hint" }, "Loading…"));
    }
    let found: Connection[];
    try {
      found = (await api<{ connections: Connection[] }>("/connections")).connections;
    } catch (e) {
      if (mine !== drawing) return;
      clear(foot);
      clear(body, h("p", { class: "status-error" }, (e as Error).message));
      foot.append(button("Close", close));
      return;
    }
    if (mine !== drawing) return;
    clear(foot);
    const row = (c: Connection): HTMLElement => {
      const path = encodeURIComponent(c.name);
      const readOnly = c.source === "pack";
      return h(
        "li",
        { class: "connection-row" },
        h(
          "div",
          { class: "connection-text" },
          h("div", { class: "pack-title" }, h("strong", {}, c.name), h("span", { class: "badge" }, c.type ? plain(c.type) : { database: "Database", file: "File", vault: "Key vault" }[c.connection]), catalogWord(c)),
          c.description ? h("div", { class: "hint" }, c.description) : null,
          c.entries
            ? h("div", { class: "vault-entries" }, c.every ? h("span", { class: "badge" }, "Every secret") : null, ...c.entries.map((e) => h("code", { class: "hint", title: `${e.secret} in the vault` }, `${e.env} ← ${e.secret}`)))
            : h("code", { class: "hint" }, `sag.connection("${c.name}")  ·  %%sql ${/\s/.test(c.name) ? `"${c.name}"` : c.name}`),
        ),
        h(
          "span",
          { class: "connection-actions" },
          readOnly ? null : button("Edit", () => void showForm({ edit: c.name }), { icon: "edit" }),
          button("Test", () => void testSaved(c.name), { icon: "play" }),
          c.has_catalog ? button("Refresh catalog", () => void refresh(c.name), { icon: "import" }) : null,
          c.entries ? null : button("Copy for Python", () => void navigator.clipboard.writeText(`conn = sag.connection("${c.name}")`).then(() => toast("Copied")), { icon: "copy", iconOnly: true }),
          readOnly
            ? null
            : button("Remove", () => {
                if (!confirm(c.entries ? `Remove the key vault ${c.name}? Its variables stop being set.` : `Remove the connection ${c.name}? Its saved passwords go too.`)) return;
                void api(`/blades/${path}`, "DELETE").then(() => showList(), (e: Error) => toast(e.message, "error"));
              }, { icon: "trash", iconOnly: true }),
        ),
      );
    };
    const databases = found.filter((c) => c.connection === "database");
    const files = found.filter((c) => c.connection === "file");
    const vaultList = found.filter((c) => c.connection === "vault");
    clear(
      body,
      found.length
        ? h(
            "div",
            {},
            databases.length ? h("h3", {}, "Databases") : null,
            databases.length ? h("ul", { class: "connection-list" }, ...databases.map(row)) : null,
            files.length ? h("h3", {}, "Files") : null,
            files.length ? h("ul", { class: "connection-list" }, ...files.map(row)) : null,
            vaultList.length ? h("h3", {}, "Key vaults") : null,
            vaultList.length ? h("ul", { class: "connection-list" }, ...vaultList.map(row)) : null,
          )
        : h("p", { class: "empty-note" }, "No connections"),
    );
    foot.append(button("Add connection", () => void showTypes(), { icon: "plus", class: "primary" }), button("Close", close));
  };

  const testSaved = async (name: string): Promise<void> => {
    toast(`Testing ${name}…`);
    try {
      const r = await api<{ type?: string; found?: string; variables?: string[] }>(`/blades/${encodeURIComponent(name)}/test`, "POST");
      toast(`${name} works: ${worked(r)}.`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const refresh = async (name: string): Promise<void> => {
    toast(`Refreshing ${name}…`);
    try {
      const r = await api<{ catalog: { tables: number; columns: number } }>(`/blades/${encodeURIComponent(name)}/refresh`, "POST");
      toast(`${name}: ${r.catalog.tables} tables, ${r.catalog.columns} columns`);
      void showList();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  // ===== ADD: the types =====
  const showTypes = async (then?: Then): Promise<void> => {
    listShowing = null;
    drawing++; // a list draw still on its way paints nothing now
    clear(foot);
    clear(body, h("p", { class: "hint" }, "Loading…"));
    let types: Form[];
    try {
      types = (await api<{ types: Form[] }>("/connections/types")).types;
    } catch (e) {
      clear(body, h("p", { class: "status-error" }, (e as Error).message));
      return;
    }
    const icons = { database: "sql", file: "folder", vault: "key" } as const;
    const tile = (t: Form): HTMLElement => button(plain(t.label), () => void showForm({ type: t, then }), { class: "connection-type", icon: icons[t.connection] });
    const group = (title: string, kind: Form["connection"]): HTMLElement[] => [h("h3", {}, title), h("div", { class: "connection-types" }, ...types.filter((t) => t.connection === kind).map(tile))];
    // From a password's New vault: only vaults, and Back returns to that connection.
    clear(body, ...(then ? [] : [...group("Databases", "database"), ...group("Files", "file")]), ...group("Key vaults", "vault"));
    foot.append(button("Back", () => void (then ? then(null) : showList()), { icon: "back" }), button("Close", close));
  };

  // ===== FORM =====
  const showForm = async (what: { type?: Form; edit?: string; then?: Then }): Promise<void> => {
    listShowing = null;
    drawing++;
    clear(foot);
    let form: Form;
    if (what.edit) {
      try {
        form = await api<Form>(`/connections/${encodeURIComponent(what.edit)}`);
      } catch (e) {
        toast((e as Error).message, "error");
        return;
      }
    } else form = structuredClone(what.type!);
    const editing = what.edit ?? null;
    const isVault = form.connection === "vault";
    let code = form.code;
    let fields = form.fields;
    // A database is named after its type until renamed; a file connection after the file chosen.
    const name = h("input", { type: "text", value: form.name ?? (form.connection === "file" ? "" : plain(form.label)), "aria-label": "Name", maxlength: 64 });
    let named = Boolean(form.name);
    name.addEventListener("input", () => (named = true));
    const description = h("input", { type: "text", value: form.description ?? "", "aria-label": "Description", placeholder: "Optional" });
    const fieldsHost = h("div", { class: "connection-fields" });
    const passwords = new Map<string, HTMLInputElement>();
    const codeBox = h("textarea", { class: "mono connection-code", rows: 8, spellcheck: false, "aria-label": "The Python this connection runs" }, code) as HTMLTextAreaElement;
    const result = h("div", { class: "connection-result", role: "status" });

    /** A changed field's value, written into the code by the server; the fields keep their boxes. */
    const fill = async (field: Field, value: string): Promise<void> => {
      try {
        const answer = await api<{ code: string; settings: Field[] }>("/blades/fill", "POST", { code, values: { [field.id]: field.type === "number" ? Number(value) : value }, template: form.id || null, name: editing });
        code = answer.code;
        codeBox.value = code;
        // The ids follow the new code; the same settings in the same order keep their boxes.
        if (answer.settings.length === fields.length) fields = fields.map((f, i) => ({ ...f, ...answer.settings[i]!, picker: f.picker }));
        else drawFields(answer.settings.map((s) => ({ ...s, picker: fields.find((f) => f.name === s.name)?.picker ?? null })));
        readWhenReady();
      } catch (e) {
        result.replaceChildren(h("p", { class: "status-error" }, (e as Error).message));
      }
    };

    const drawFields = (next: Field[]): void => {
      fields = next;
      const boxes = fields.map((field, i) => {
        if (field.type === "flag") {
          // On or off: True/False in the code, yes/no in a connection string.
          const tick = h("input", { type: "checkbox", checked: ["true", "yes", "1", "on"].includes(String(field.value).toLowerCase()), "aria-label": words(field.name) }) as HTMLInputElement;
          tick.addEventListener("change", () => void fill(fields[i]!, tick.checked ? "yes" : "no"));
          return h("label", { class: "field field-flag" }, tick, h("span", {}, words(field.name)));
        }
        const input = h("input", { type: field.type === "number" ? "number" : "text", value: field.placeholder ? "" : String(field.value), placeholder: String(field.value), "aria-label": words(field.name), spellcheck: false });
        input.addEventListener("change", () => void fill(fields[i]!, input.value));
        const browse = field.picker
          ? button("Browse…", () => {
              const suffixes = field.name === "certificate file" ? [".pem", ".crt", ".cer"] : FILE_KINDS[form.id];
              void chooseFile({ title: field.picker === "folder" ? "Choose folder" : "Choose file", mode: field.picker!, suffixes, start: field.placeholder ? undefined : String(field.value) }).then((path) => {
                if (!path) return;
                input.value = path;
                if (!named) name.value = stem(path);
                void fill(fields[i]!, path);
              });
            }, { icon: "folder" })
          : null;
        const list = field.suggestions.length ? h("datalist", { id: `conn-suggest-${i}` }, ...field.suggestions.map((s) => h("option", { value: s.value }, s.from))) : null;
        if (list) input.setAttribute("list", `conn-suggest-${i}`);
        return h("label", { class: `field${field.picker ? " wide" : ""}` }, h("span", {}, words(field.name), field.key.toLowerCase() !== field.name.toLowerCase() ? h("span", { class: "hint" }, ` (${field.key})`) : null), h("span", { class: "field-row" }, input, browse), list);
      });
      clear(fieldsHost, ...boxes);
    };
    drawFields(fields);

    // Each password's source: saved on this computer (the keyring), an environment variable, or a key
    // vault (a vault blade's entry, or a new one added to it). Variables and vault entries are named in
    // the code: secret("WAREHOUSE_PASSWORD"), read from the environment the vault sets.
    let vaults: Vault[] = [];
    const readVaults = (): Promise<unknown> => api<{ vaults: Vault[] }>("/connections/vaults").then((r) => (vaults = r.vaults), () => undefined);
    const vaultsRead = readVaults();
    // Each password's source as the user chose it (kept when the passwords are drawn again).
    const chosen = new Map<string, string>();
    // A password pointed at a vault: which vault, and the secret's name there (shown when drawn again).
    const pointedAt = new Map<string, string>();
    const secretOf = new Map<string, string>();
    const looksLikeVariable = (s: string): boolean => /^[A-Z][A-Z0-9_]*$/.test(s) && s.includes("_");
    // Each password keeps the word the starter gave it ("Password") while its source renames it in the code.
    const labels = new Map<string, string>(form.secrets.map((s) => [s, s]));
    const label = (s: string): string => words(labels.get(s) ?? s);
    const pointAt = async (secret: string, body: Record<string, unknown>): Promise<void> => {
      try {
        const answer = await api<{ code: string; secret: string }>("/connections/source", "POST", { code, secret, label: labels.get(secret) ?? secret, connection: name.value.trim(), ...body });
        code = answer.code;
        codeBox.value = code;
        form.secrets = form.secrets.map((s) => (s === secret ? answer.secret : s));
        labels.set(answer.secret, labels.get(secret) ?? secret);
        if (body.from === "vault") vaults = (await api<{ vaults: Vault[] }>("/connections/vaults")).vaults;
        chosen.delete(secret);
        chosen.set(answer.secret, String(body.from)); // drawn again as the source just chosen
        if (body.vault) pointedAt.set(answer.secret, String(body.vault));
        if (typeof body.vault_secret === "string") secretOf.set(answer.secret, body.vault_secret);
        passwordsSlot.replaceChildren(drawPasswords() ?? "");
        result.replaceChildren(h("p", { class: "hint" }, `${label(answer.secret)}: ${answer.secret}${body.vault ? ` in ${String(body.vault)}` : ""}`));
      } catch (e) {
        result.replaceChildren(h("p", { class: "status-error" }, (e as Error).message));
      }
    };
    const drawPasswords = (): HTMLElement | null => {
      passwords.clear();
      if (!form.secrets.length) return null;
      const saved = form.summary?.secrets ?? {};
      // Which vault each password was pointed at (an every-secret vault has no entry to show it by).
      return h(
        "fieldset",
        { class: "connection-passwords" },
        h("legend", {}, "Passwords"),
        ...form.secrets.map((s) => {
          const inVault = vaults.find((v) => v.entries.some((e) => e.env === s)) ?? vaults.find((v) => v.name === pointedAt.get(s));
          const origin = chosen.get(s) ?? (inVault ? "vault" : looksLikeVariable(s) && !saved[s] ? "env" : "keyring");
          const from = h("select", { "aria-label": `Where ${label(s)} comes from` }, h("option", { value: "keyring", selected: origin === "keyring" }, "Saved on this computer"), h("option", { value: "env", selected: origin === "env" }, "An environment variable"), h("option", { value: "vault", selected: origin === "vault" }, "A key vault"));
          const slot = h("span", { class: "field-row" });
          const draw = (): void => {
            if (from.value === "keyring") {
              const input = h("input", { type: "password", autocomplete: "new-password", "aria-label": label(s), placeholder: saved[s] ? "Saved" : "" });
              passwords.set(s, input);
              slot.replaceChildren(input);
            } else if (from.value === "env") {
              passwords.delete(s);
              const variable = h("input", { type: "text", class: "mono", "aria-label": `${label(s)}: the variable's name`, value: looksLikeVariable(s) ? s : "", placeholder: "WAREHOUSE_PASSWORD", spellcheck: false });
              slot.replaceChildren(variable, button("Use it", () => void pointAt(s, { from: "env", env: variable.value.trim() }), { icon: "check" }));
            } else {
              passwords.delete(s);
              if (!vaults.length) {
                slot.replaceChildren(button("New vault", () => newVault(s), { icon: "plus" }));
                return;
              }
              const vault = h("select", { "aria-label": "Vault" }, ...vaults.map((v) => h("option", { value: v.name, selected: v === inVault }, v.name)));
              const entry = h("select", { "aria-label": "Entry in the vault" });
              const newName = h("input", { type: "text", "aria-label": "The secret's name in the vault", placeholder: "Secret name", spellcheck: false, value: pointedAt.get(s) ? (secretOf.get(s) ?? "") : "" });
              const fillEntries = (): void => {
                const chosen = vaults.find((v) => v.name === vault.value);
                clear(entry, ...(chosen?.entries ?? []).map((e) => h("option", { value: e.env, selected: e.env === s }, `${e.secret} (as ${e.env})`)), chosen?.read_only ? null : h("option", { value: "", selected: !chosen?.entries.some((e) => e.env === s) }, chosen?.every ? "A secret by name…" : "A new entry…"));
                newName.hidden = entry.value !== "";
              };
              vault.addEventListener("change", fillEntries);
              entry.addEventListener("change", () => (newName.hidden = entry.value !== ""));
              fillEntries();
              slot.replaceChildren(vault, entry, newName, button("Use it", () => void pointAt(s, entry.value ? { from: "vault", vault: vault.value, env: entry.value } : { from: "vault", vault: vault.value, vault_secret: newName.value.trim() }), { icon: "check" }));
            }
          };
          from.addEventListener("change", () => {
            chosen.set(s, from.value);
            draw();
          });
          draw();
          return h("div", { class: "field connection-password" }, h("span", {}, label(s)), h("span", { class: "field-row" }, from, slot));
        }),
      );
    };
    void vaultsRead.then(() => passwordsSlot.replaceChildren(drawPasswords() ?? ""));

    /** A vault made from here: this form waits (kept as it is) and comes back with the new vault in its list. */
    const newVault = (secret: string): void => {
      const kept = [...body.childNodes];
      const keptFoot = [...foot.childNodes];
      chosen.set(secret, "vault");
      void showTypes(async () => {
        body.replaceChildren(...kept);
        foot.replaceChildren(...keptFoot);
        await readVaults();
        passwordsSlot.replaceChildren(drawPasswords() ?? "");
      });
    };

    // ===== a vault's secrets: each ticked one becomes an environment variable =====
    // Every secret (a new vault's default): each one under its own name, new ones too; a box renames one.
    const every = h("input", { type: "checkbox", checked: editing ? Boolean(form.vault_all) : true, "aria-label": "Every secret in the vault" }) as HTMLInputElement;
    const mapRows: { secret: string; env: HTMLInputElement; use: HTMLInputElement; row: HTMLElement }[] = [];
    const mapBody = h("tbody", {});
    const mapFilter = h("input", { type: "search", placeholder: "Filter", "aria-label": "Filter the secrets", hidden: true }) as HTMLInputElement;
    /** A secret's row. `mapped`: the variable it was given; none, and it takes the vault's own name. */
    const addMapping = (secret: string, mapped: string | null, use: boolean): void => {
      if (mapRows.some((r) => r.secret === secret)) return;
      const tick = h("input", { type: "checkbox", checked: use, "aria-label": `Use ${secret}` }) as HTMLInputElement;
      const own = envFor(secret);
      const variable = h("input", { type: "text", class: "mono", value: mapped && mapped !== own ? mapped : "", placeholder: own, spellcheck: false, "aria-label": `${secret}: the variable's name` }) as HTMLInputElement;
      variable.addEventListener("input", () => (tick.checked = true));
      const row = h("tr", {}, h("td", {}, tick), h("td", {}, h("code", {}, secret)), h("td", {}, variable));
      mapRows.push({ secret, env: variable, use: tick, row });
      mapBody.insertBefore(row, newRow);
      mapFilter.hidden = mapRows.length <= 10;
    };
    const newSecret = h("input", { type: "text", placeholder: "Secret name", spellcheck: false, "aria-label": "A secret's name in the vault" }) as HTMLInputElement;
    const addTyped = (): void => {
      const secret = newSecret.value.trim();
      if (!secret) return;
      addMapping(secret, null, true);
      newSecret.value = "";
      newSecret.focus();
    };
    newSecret.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        addTyped();
      }
    });
    const newRow = h("tr", { class: "vault-new" }, h("td", {}), h("td", {}, newSecret), h("td", {}, button("Add", addTyped, { icon: "plus" })));
    mapBody.append(newRow);
    mapFilter.addEventListener("input", () => {
      const words = mapFilter.value.toLowerCase();
      for (const r of mapRows) r.row.hidden = Boolean(words) && !`${r.secret} ${r.env.value} ${r.env.placeholder}`.toLowerCase().includes(words);
    });
    for (const m of form.vault_mapping ?? []) if (editing) addMapping(m.secret, m.env, true);
    const mapTable = h("table", { class: "vault-map" }, h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "In the vault"), h("th", {}, "Environment variable"))), mapBody);
    const showEvery = (): void => void mapTable.classList.toggle("every", every.checked);
    every.addEventListener("change", showEvery);
    showEvery();
    let reading = 0;
    const readVault = async (): Promise<void> => {
      const mine = ++reading;
      result.replaceChildren(h("p", { class: "hint" }, "Reading the vault…"));
      try {
        const r = await api<{ names: Mapping[] | null }>("/connections/vaults/secrets", "POST", { ...sent(), secrets: typed() });
        if (mine !== reading) return; // a newer read (the address changed again) answers instead
        if (r.names === null) {
          result.replaceChildren(h("p", { class: "hint" }, "This vault does not list its secrets: add them by name."));
          newSecret.focus();
          return;
        }
        for (const m of r.names) addMapping(m.secret, null, false);
        result.replaceChildren(h("p", { class: "status-ok" }, `${r.names.length} secret${r.names.length === 1 ? "" : "s"}`));
      } catch (e) {
        if (mine === reading) failed(e);
      }
    };
    /** Read the vault on its own once it can be: every setting filled in (not a starter's placeholder). */
    const readWhenReady = (): void => {
      if (isVault && fields.every((f) => !f.placeholder)) void readVault();
    };
    const secretsBox = isVault
      ? h(
          "fieldset",
          { class: "connection-passwords vault-secrets" },
          h("legend", {}, "Secrets"),
          h("label", { class: "vault-every" }, every, h("span", {}, "Every secret in the vault")),
          h("div", { class: "field-row" }, button("Read the vault", () => void readVault(), { icon: "import" }), mapFilter),
          mapTable,
        )
      : null;

    // The code edited by hand: read its fields and passwords again.
    codeBox.addEventListener("change", () => {
      code = codeBox.value;
      void api<{ settings: Field[]; secrets: string[]; install_here: string | null }>("/blades/inspect", "POST", { code, template: form.id || null, name: editing }).then(
        (found) => {
          // The packages the code needs now (a stale "Not installed" once stayed after the code changed).
          form.install_here = found.install_here;
          drawInstall();
          drawFields(found.settings.map((s) => ({ ...s, picker: fields.find((f) => f.name === s.name)?.picker ?? null })));
          if (found.secrets.join() !== form.secrets.join()) {
            form.secrets = found.secrets;
            passwordsSlot.replaceChildren(drawPasswords() ?? "");
          }
          result.replaceChildren();
          readWhenReady();
        },
        (e: Error) => result.replaceChildren(h("p", { class: "status-error" }, e.message)),
      );
    });

    const typed = (): Record<string, string> => Object.fromEntries([...passwords].filter(([, input]) => input.value).map(([s, input]) => [s, input.value]));
    const sent = (): Record<string, unknown> => ({
      name: name.value.trim(),
      description: description.value.trim(),
      kind: form.kind,
      template: form.id || null,
      dialect: form.dialect,
      variable: form.variable,
      code,
      catalog: form.catalog,
      // Every secret: only the renamed ones are kept (the rest take their own names each time).
      ...(isVault
        ? {
            vault_all: every.checked,
            // A box left empty: the vault's own name. Every secret keeps only the ones given another.
            vault_mapping: mapRows
              .filter((r) => (every.checked ? r.env.value.trim() !== "" && r.env.value.trim() !== envFor(r.secret) : r.use.checked))
              .map((r) => ({ env: r.env.value.trim() || envFor(r.secret), secret: r.secret })),
          }
        : {}),
    });
    const failed = (e: unknown): void => {
      const message = (e as Error).message;
      result.replaceChildren(h("p", { class: "status-error" }, message));
    };
    const test = async (): Promise<void> => {
      result.replaceChildren(h("p", { class: "hint" }, isVault ? "Reading…" : "Connecting…"));
      try {
        const r = await api<{ type?: string; found?: string; variables?: string[] }>("/connections/test", "POST", { ...sent(), secrets: typed() });
        result.replaceChildren(h("p", { class: "status-ok" }, `It works: ${worked(r)}.`));
      } catch (e) {
        failed(e);
      }
    };
    const save = async (): Promise<void> => {
      try {
        const body = { ...sent(), secrets: typed() };
        await (editing ? api(`/connections/${encodeURIComponent(editing)}`, "PUT", body) : api("/connections", "POST", body));
        toast(`Saved ${name.value.trim()}.`);
        if (what.then) what.then(name.value.trim());
        else void showList();
      } catch (e) {
        failed(e);
      }
    };

    // Sign in (SQL Server, Azure SQL): each way is its own code; switching keeps the values typed so far.
    const ways = form.sign_ins ?? [];
    const current = Math.max(0, ways.findIndex((w) => code.toLowerCase().includes(w.mark.toLowerCase())));
    const signInSelect = h("select", { "aria-label": "Sign in" }, ...ways.map((w, i) => h("option", { value: String(i), selected: i === current }, w.label))) as HTMLSelectElement;
    signInSelect.addEventListener("change", () => void switchSignIn(ways[Number(signInSelect.value)]!));
    const signIn = ways.length > 1 ? h("label", { class: "field" }, h("span", {}, "Sign in"), signInSelect) : null;
    const switchSignIn = async (way: { code: string }): Promise<void> => {
      const typedSoFar = new Map(fields.filter((f) => !f.placeholder).map((f) => [f.name, f.value]));
      try {
        const next = await api<{ settings: Field[]; secrets: string[] }>("/blades/inspect", "POST", { code: way.code, template: form.id || null, name: editing });
        const keep = Object.fromEntries(next.settings.filter((s) => typedSoFar.has(s.name) && s.name !== "driver").map((s) => [s.id, typedSoFar.get(s.name)]));
        const answer = Object.keys(keep).length
          ? await api<{ code: string; settings: Field[] }>("/blades/fill", "POST", { code: way.code, values: keep, template: form.id || null, name: editing })
          : { code: way.code, settings: next.settings };
        code = answer.code;
        codeBox.value = code;
        drawFields(answer.settings.map((s) => ({ ...s, picker: fields.find((f) => f.name === s.name)?.picker ?? null })));
        form.secrets = next.secrets;
        labels.clear();
        for (const secret of next.secrets) labels.set(secret, secret);
        passwordsSlot.replaceChildren(drawPasswords() ?? "");
        result.replaceChildren();
      } catch (e) {
        failed(e);
      }
    };
    const passwordsSlot = h("div", {}, drawPasswords() ?? "");
    const installSlot = h("div", {});
    const drawInstall = (): void =>
      void installSlot.replaceChildren(...(form.install_here ? [h("p", { class: "hint" }, isVault ? "Not installed: " : "Driver not installed: ", h("code", {}, form.install_here))] : []));
    drawInstall();
    clear(
      body,
      h("h3", {}, editing ? `Edit ${editing}` : isVault ? `New ${plain(form.label)}` : `New ${plain(form.label)} connection`),
      form.note ? h("p", { class: "hint" }, form.note) : null,
      h("label", { class: "field" }, h("span", {}, "Name"), name),
      h("label", { class: "field" }, h("span", {}, "Description"), description),
      signIn,
      fieldsHost,
      passwordsSlot,
      secretsBox,
      installSlot,
      result,
      h("details", { class: "connection-code-box" }, h("summary", {}, "Python code"), codeBox),
    );
    foot.append(
      button("Back", () => void (editing ? showList() : showTypes(what.then)), { icon: "back" }),
      button(isVault ? "Test" : "Test connection", () => void test(), { icon: "play" }),
      button("Save", () => void save(), { icon: "save", class: "primary" }),
    );
    name.focus();
    name.select();
    readWhenReady(); // a vault opened to edit is read at once; a new one when its address is filled in
  };

  await showList();
}
