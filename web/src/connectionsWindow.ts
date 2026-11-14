/**
 * connectionsWindow.ts: the Application row's Connections window (Plans/workbench-and-connections.plan.md):
 * the user's connections, each a database, a file or a key vault, and adding one with a form per type.
 * [WIRE + EXPORT]
 *
 *   [LIST]   each connection: its type, its catalog, Edit, Test, Refresh catalog, Copy for Python, Remove;
 *            a key vault shows its entries (variable <- secret in the vault) instead
 *   [ADD]    pick a type (Databases, Files, Vaults), then its form:
 *   [FORM]   a name; the starter's settings as fields (a file or folder field has Browse…); its
 *            passwords (kept in the system keyring); Test connection (runs the form, nothing saved);
 *            Save. The Python it makes shows underneath, editable for the unusual case
 *   [VAULT]  a vault's form adds its secrets: Every secret (each under its own name, new ones too), or
 *            the ones ticked; Read the vault lists their names (never a value) and a variable's box
 *            renames one. Every Workbench kernel starts with them set
 *
 * ROUTING: [LIST]  GET /connections; Remove -> DELETE /blades/{name}; Refresh -> POST /blades/{name}/refresh
 *          [TYPES] GET /connections/types        [EDIT] GET /connections/{name}
 *          [FIELD] POST /blades/fill (the value into the code)   [CODE] POST /blades/inspect (its fields again)
 *          [TEST]  POST /connections/test        [SAVE] POST /connections, PUT /connections/{name}
 *          [VAULT] POST /connections/vaults/secrets (the names in it); GET /connections/vaults (a password's)
 */
import { api } from "./api";
import { button, clear, h, openDialog, toast } from "./dom";
import { chooseFile } from "./fileChooser";

interface Field {
  id: string;
  name: string;
  key: string;
  value: string | number | boolean;
  type: "text" | "number";
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

/** The Application row's button. */
export function connectionsButton(): HTMLButtonElement {
  return button("Connections", () => void openConnections(), { icon: "link", class: "ribbon-btn" });
}

export async function openConnections(): Promise<void> {
  const { body, foot, close } = openDialog("Connections", true);
  const showList = async (): Promise<void> => {
    clear(foot);
    clear(body, h("p", { class: "hint" }, "Loading…"));
    let found: Connection[];
    try {
      found = (await api<{ connections: Connection[] }>("/connections")).connections;
    } catch (e) {
      clear(body, h("p", { class: "status-error" }, (e as Error).message));
      foot.append(button("Close", close));
      return;
    }
    const row = (c: Connection): HTMLElement => {
      const path = encodeURIComponent(c.name);
      const readOnly = c.source === "pack";
      return h(
        "li",
        { class: "connection-row" },
        h(
          "div",
          { class: "connection-text" },
          h("div", { class: "pack-title" }, h("strong", {}, c.name), h("span", { class: "badge" }, c.type ? plain(c.type) : { database: "Database", file: "File", vault: "Key vault" }[c.connection]), c.catalog_loaded ? h("span", { class: "hint" }, "catalog loaded") : null),
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
      } catch (e) {
        result.replaceChildren(h("p", { class: "status-error" }, (e as Error).message));
      }
    };

    const drawFields = (next: Field[]): void => {
      fields = next;
      const boxes = fields.map((field, i) => {
        const input = h("input", { type: field.type === "number" ? "number" : "text", value: field.placeholder ? "" : String(field.value), placeholder: String(field.value), "aria-label": words(field.name), spellcheck: false });
        input.addEventListener("change", () => void fill(fields[i]!, input.value));
        const browse = field.picker
          ? button("Browse…", () => {
              void chooseFile({ title: field.picker === "folder" ? "Choose folder" : "Choose file", mode: field.picker!, suffixes: FILE_KINDS[form.id], start: field.placeholder ? undefined : String(field.value) }).then((path) => {
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
    const addMapping = (secret: string, env: string, use: boolean): void => {
      if (mapRows.some((r) => r.secret === secret)) return;
      const tick = h("input", { type: "checkbox", checked: use, "aria-label": `Use ${secret}` }) as HTMLInputElement;
      const variable = h("input", { type: "text", class: "mono", value: env, spellcheck: false, "aria-label": `${secret}: the variable's name` }) as HTMLInputElement;
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
      addMapping(secret, envFor(secret), true);
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
      for (const r of mapRows) r.row.hidden = Boolean(words) && !`${r.secret} ${r.env.value}`.toLowerCase().includes(words);
    });
    for (const m of form.vault_mapping ?? []) if (editing) addMapping(m.secret, m.env, true);
    const mapTable = h("table", { class: "vault-map" }, h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "In the vault"), h("th", {}, "Environment variable"))), mapBody);
    const showEvery = (): void => void mapTable.classList.toggle("every", every.checked);
    every.addEventListener("change", showEvery);
    showEvery();
    const readVault = async (): Promise<void> => {
      result.replaceChildren(h("p", { class: "hint" }, "Reading the vault…"));
      try {
        const r = await api<{ names: Mapping[] | null }>("/connections/vaults/secrets", "POST", { ...sent(), secrets: typed() });
        if (r.names === null) {
          result.replaceChildren(h("p", { class: "hint" }, "This vault does not list its secrets: add them by name."));
          newSecret.focus();
          return;
        }
        for (const m of r.names) addMapping(m.secret, m.env, false);
        result.replaceChildren(h("p", { class: "status-ok" }, `${r.names.length} secret${r.names.length === 1 ? "" : "s"}`));
      } catch (e) {
        failed(e);
      }
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
      void api<{ settings: Field[]; secrets: string[] }>("/blades/inspect", "POST", { code, template: form.id || null, name: editing }).then(
        (found) => {
          drawFields(found.settings.map((s) => ({ ...s, picker: fields.find((f) => f.name === s.name)?.picker ?? null })));
          if (found.secrets.join() !== form.secrets.join()) {
            form.secrets = found.secrets;
            passwordsSlot.replaceChildren(drawPasswords() ?? "");
          }
          result.replaceChildren();
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
            vault_mapping: mapRows.filter((r) => (every.checked ? r.env.value.trim() !== envFor(r.secret) : r.use.checked)).map((r) => ({ env: r.env.value.trim(), secret: r.secret })),
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

    const passwordsSlot = h("div", {}, drawPasswords() ?? "");
    clear(
      body,
      h("h3", {}, editing ? `Edit ${editing}` : isVault ? `New ${plain(form.label)}` : `New ${plain(form.label)} connection`),
      form.note ? h("p", { class: "hint" }, form.note) : null,
      h("label", { class: "field" }, h("span", {}, "Name"), name),
      h("label", { class: "field" }, h("span", {}, "Description"), description),
      fieldsHost,
      passwordsSlot,
      secretsBox,
      form.install_here ? h("p", { class: "hint" }, isVault ? "Not installed: " : "Driver not installed: ", h("code", {}, form.install_here)) : null,
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
  };

  await showList();
}
