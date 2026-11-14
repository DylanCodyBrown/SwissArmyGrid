/**
 * bladeLibrary.ts: portable blades on the page: import (review first), export, blade folders. [VIEW]
 *
 * A portable blade is a plain Python file whose frontmatter is a `# /// swissarmygrid-blade` header
 * (blade_core/portable.py). Import shows what each file holds, its full code and the secrets it will
 * ask for, and whether its name is taken (replace, keep both or skip), before anything is saved;
 * nothing runs. Blade folders are searched for such files; a folder blade's file is its own.
 */
import { api, download } from "./api";
import { append, button, clear, h, openDialog, toast } from "./dom";
import { type BladeKind, type BladeSetting, kindLabel, settingField } from "./bladeEditor";

interface ImportedBlade {
  name: string;
  kind: BladeKind;
  code?: string;
  description?: string;
  tags?: string[];
}

interface PreviewItem {
  index: number;
  file: string;
  error: string | null;
  blade?: ImportedBlade;
  secrets?: string[];
  conflict?: { name: string; source: "saved" | "folder" } | null;
  settings?: BladeSetting[];
}

interface ImportResult {
  file: string;
  name?: string;
  status: "saved" | "skipped" | "error";
  message?: string;
  path?: string | null;
}

export interface BladeFolder {
  path: string;
  from_env: boolean;
  exists: boolean;
  blades?: number;
}

/** The blade folders, and the files in them that have a blade header but could not be read. */
export async function bladeFolders(): Promise<{ folders: BladeFolder[]; problems: { path: string; error: string }[] }> {
  return api("/blades/folders");
}

async function asBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}

function save(blob: Blob, name: string): void {
  const link = h("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
}

/** Download one blade as its .blade.py file, or every blade as a zip. */
export async function exportBlades(name?: string): Promise<void> {
  try {
    const got = await download(name ? `/blades/${encodeURIComponent(name)}/export` : "/blades/export");
    save(got.blob, got.name);
    toast(name ? `Exported ${name} as ${got.name}.` : `Exported every blade as ${got.name}.`);
  } catch (error) {
    toast((error as Error).message, "error");
  }
}

/** Ask for files (.blade.py, .py or .zip), then review and import them. */
export function chooseBladeFiles(onDone: () => void): void {
  const picker = h("input", { type: "file", multiple: true, accept: ".py,.zip", hidden: true }) as HTMLInputElement;
  picker.addEventListener("change", () => {
    const files = [...(picker.files ?? [])];
    picker.remove();
    if (files.length) void importBlades(files, onDone);
  });
  document.body.append(picker);
  picker.click();
}

/** Review the blades in these files, then import the ones chosen. Nothing is saved before Import, and
 * nothing runs at all: a blade's code runs only when the blade is used. */
export async function importBlades(files: File[], onDone: () => void): Promise<void> {
  let sent: { name: string; data: string }[];
  let items: PreviewItem[];
  try {
    sent = await Promise.all(files.map(async (f) => ({ name: f.name, data: await asBase64(f) })));
    items = (await api<{ items: PreviewItem[] }>("/blades/import", "POST", { files: sent })).items;
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  const { body, foot, close } = openDialog("Import blades", true);
  const choices: Record<string, string> = {};
  // What the person fills in per blade: new values for its settings, and the secrets it asks for.
  const fill: Filled = {};
  body.append(h("p", { class: "hint" }, "Check the code before importing."));
  for (const item of items) {
    const card = h("section", { class: "import-card" });
    if (!item.blade) {
      card.append(h("h3", {}, item.file), h("p", { class: "status-error" }, item.error ?? "Not a blade."));
      body.append(card);
      continue;
    }
    const blade = item.blade;
    choices[String(item.index)] = item.conflict ? "copy" : "save";
    const conflict = item.conflict
      ? (() => {
          const select = h("select", { "aria-label": `When ${blade.name} is taken` });
          for (const [value, label] of [
            ["copy", `Keep both (save as ${blade.name} 2)`],
            ["replace", `Replace the ${item.conflict.source === "folder" ? "folder" : "saved"} blade`],
            ["skip", "Skip"],
          ]) {
            select.append(h("option", { value }, label));
          }
          select.addEventListener("change", () => (choices[String(item.index)] = select.value));
          return h("label", { class: "warning-text" }, `${item.conflict.name} already exists: `, select);
        })()
      : null;
    append(
      card,
      h("h3", {}, h("span", { class: `blade-kind ${blade.kind}` }, kindLabel(blade.kind)), " ", blade.name, h("span", { class: "hint" }, `  ${item.file}`)),
      blade.description ? h("p", {}, blade.description) : null,
      blade.tags?.length ? h("div", { class: "chips" }, ...blade.tags.map((t) => h("span", { class: "chip" }, t))) : null,
      fillIn(item, fill),
      conflict,
      blade.code ? h("details", {}, h("summary", {}, "The code"), h("pre", { class: "code-box" }, blade.code)) : null,
    );
    body.append(card);
  }
  const where = h("select", { "aria-label": "Save the blades in" }, h("option", { value: "" }, "This app"));
  try {
    for (const folder of (await bladeFolders()).folders) if (folder.exists) where.append(h("option", { value: folder.path }, `Blade folder: ${folder.path}`));
  } catch {
    // no folders to offer
  }
  const importable = items.filter((i) => i.blade).length;
  body.append(h("label", { class: "field" }, h("span", {}, "Save in"), where));
  foot.append(
    button("Cancel", close, { class: "ghost" }),
    button(`Import ${importable} blade${importable === 1 ? "" : "s"}`, () => {
      void api<{ results: ImportResult[] }>("/blades/import", "POST", { files: sent, apply: true, choices, fill, folder: where.value || null }).then(
        (answer) => {
          close();
          const saved = answer.results.filter((r) => r.status === "saved");
          const failed = answer.results.filter((r) => r.status === "error");
          toast(`Imported ${saved.length} blade${saved.length === 1 ? "" : "s"}${saved.length ? ": " + saved.map((r) => r.name).join(", ") : ""}.`);
          for (const r of failed) toast(`${r.name ?? r.file}: ${r.message ?? "not imported"}`, "error");
          for (const r of saved) if (r.message) toast(`${r.name}: ${r.message}`, "error");
          onDone();
        },
        (error: Error) => toast(error.message, "error"),
      );
    }, { class: "primary", icon: "plus" }),
  );
  if (!importable) foot.querySelector<HTMLButtonElement>("button.primary")!.disabled = true;
}

/** The values this blade needs from the person importing it: its settings (server, database, path...)
 * with suggestions, and the secrets its code asks for (kept in this computer's vault, never in a file). */
type Filled = Record<string, { settings: Record<string, string>; secrets: Record<string, string> }>;

function fillIn(item: PreviewItem, fill: Filled): HTMLElement | null {
  const settings = item.settings ?? [];
  const secrets = item.secrets ?? [];
  if (!settings.length && !secrets.length) return null;
  const mine = (fill[String(item.index)] ??= { settings: {}, secrets: {} });
  return h(
    "fieldset",
    { class: "fill-in" },
    h("legend", {}, "Your values"),
    ...settings.map((setting) =>
      settingField(setting, (value) => {
        if (value === String(setting.value)) delete mine.settings[setting.id];
        else mine.settings[setting.id] = value;
      }),
    ),
    ...secrets.map((secret) => {
      const input = h("input", { type: "password", autocomplete: "new-password", placeholder: "Optional", "aria-label": `secret ${secret}` });
      input.addEventListener("input", () => (mine.secrets[secret] = input.value));
      return h(
        "div",
        { class: "setting-row" },
        h("label", {}, h("span", { class: "setting-key" }, `secret("${secret}")`)),
        input,
      );
    }),
  );
}

/** The blade folders: add, remove, see what each holds and which files could not be read. */
export async function editBladeFolders(onDone: () => void): Promise<void> {
  let state: Awaited<ReturnType<typeof bladeFolders>>;
  try {
    state = await bladeFolders();
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  const { body, foot, close } = openDialog("Blade folders");
  const mine = state.folders.filter((f) => !f.from_env).map((f) => f.path);
  const list = h("ul", { class: "folder-list" });
  const render = (): void => {
    clear(list);
    for (const folder of state.folders) {
      if (!folder.from_env && !mine.includes(folder.path)) continue;
      list.append(
        h(
          "li",
          {},
          h("code", {}, folder.path),
          h("span", { class: "hint" }, folder.exists ? `  ${folder.blades ?? 0} blade${folder.blades === 1 ? "" : "s"}` : "  not found"),
          folder.from_env
            ? h("span", { class: "hint" }, "  (from SWISSARMYGRID_BLADE_FOLDERS)")
            : button("Remove", () => {
                mine.splice(mine.indexOf(folder.path), 1);
                render();
              }, { icon: "trash", iconOnly: true, class: "ghost" }),
        ),
      );
    }
    for (const path of mine.filter((p) => !state.folders.some((f) => f.path === p))) {
      list.append(h("li", {}, h("code", {}, path), h("span", { class: "hint" }, "  added")));
    }
  };
  const input = h("input", { type: "text", placeholder: "C:\\Users\\me\\blades", "aria-label": "Folder to add", spellcheck: "false" });
  append(
    body,
    list,
    h("div", { class: "row-actions" }, input, button("Add", () => {
      const path = input.value.trim();
      if (path && !mine.includes(path)) mine.push(path);
      input.value = "";
      render();
    }, { icon: "plus" })),
    state.problems.length
      ? h("div", {}, h("h3", {}, "Unreadable blade files"), h("ul", {}, ...state.problems.map((p) => h("li", {}, h("code", {}, p.path), h("div", { class: "status-error" }, p.error)))))
      : null,
  );
  render();
  foot.append(
    button("Cancel", close, { class: "ghost" }),
    button("Save", () => {
      void api("/blades/folders", "PUT", { folders: mine }).then(() => {
        close();
        toast("Blade folders saved.");
        onDone();
      }, (error: Error) => toast(error.message, "error"));
    }, { class: "primary", icon: "save" }),
  );
}

// ===== packs: blades and tangs from your team's Claude Code marketplace =====

interface PackInfo {
  name: string;
  marketplace: string;
  version: string;
  label: string;
  root: string;
  blades: number;
  tangs: number;
  enabled: boolean;
  editable: boolean;
}

interface PacksAnswer {
  installed: PackInfo[];
  editable: PackInfo[];
  plugins_root: string;
  commands: { add: string; install: string; update: string };
}

function commandLine(text: string): HTMLElement {
  return h(
    "div",
    { class: "row-actions" },
    h("code", { class: "command-line" }, text),
    button("Copy", () => void navigator.clipboard.writeText(text).then(() => toast("Copied.")), { icon: "copy", iconOnly: true, class: "ghost" }),
  );
}

/** The Packs window: installed packs (read-only blades and tangs), editable ones (a clone of the team's
 * repository in a blade folder) with Add blades and tangs, and a new marketplace repository. */
export async function openPacks(onDone: () => void): Promise<void> {
  let state: PacksAnswer;
  try {
    state = await api<PacksAnswer>("/packs");
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  const { body, foot, close } = openDialog("Packs", true);
  const packRow = (p: PackInfo, action: HTMLElement | null): HTMLElement =>
    h(
      "li",
      { class: "tang-item" },
      h(
        "div",
        { class: "tang-text" },
        // The name, its marketplace and its state on one line (the badge, in the column's grid, had
        // stretched to a full-width outline).
        h(
          "div",
          { class: "pack-title" },
          h("strong", {}, p.name),
          p.marketplace ? h("span", { class: "hint" }, `@ ${p.marketplace}`) : null,
          h("span", { class: "badge" }, p.editable ? "editable clone" : p.enabled ? `version ${p.version}` : "switched off in Claude Code"),
        ),
        h("div", { class: "hint" }, `${p.blades} blade${p.blades === 1 ? "" : "s"} · ${p.tangs} tang${p.tangs === 1 ? "" : "s"} · `, h("code", {}, p.root)),
      ),
      action,
    );
  append(
    body,
    h("h3", {}, "Installed"),
    state.installed.length
      ? h("ul", { class: "tang-list" }, ...state.installed.map((p) => packRow(p, null)))
      : h("p", { class: "hint" }, "No packs installed"),
    h("h3", {}, "Install in Claude Code"),
    commandLine(state.commands.add),
    commandLine(state.commands.install),
    h("p", { class: "hint" }, "Update"),
    commandLine(state.commands.update),
    h("h3", {}, "Change a pack"),
    state.editable.length
      ? h(
          "ul",
          { class: "tang-list" },
          ...state.editable.map((p) => packRow(p, button("Add blades and tangs", () => void addToPack(p, onDone), { icon: "plus", class: "primary" }))),
        )
      : h("p", { class: "hint" }, "No editable packs"),
  );
  foot.append(
    button("New marketplace repository", () => newMarketplace(() => {
      close();
      onDone();
      void openPacks(onDone);
    }), { icon: "plus" }),
    button("Close", close, { class: "primary" }),
  );
}

async function addToPack(pack: PackInfo, onDone: () => void): Promise<void> {
  let blades: { name: string; source: string }[] = [];
  let tangs: { name: string; where: string }[] = [];
  try {
    blades = (await api<{ blades: { name: string; source: string }[] }>("/blades")).blades.filter((b) => b.source !== "pack");
    tangs = (await api<{ models: { name: string; where: string }[] }>("/relationships/library")).models.filter((t) => t.where !== "pack");
  } catch (error) {
    toast((error as Error).message, "error");
    return;
  }
  const { body, foot, close } = openDialog(`Add to ${pack.name}`);
  const picks = (items: string[], label: string): [HTMLElement, () => string[]] => {
    const boxes = items.map((n) => [n, h("input", { type: "checkbox", "aria-label": `${label} ${n}` }) as HTMLInputElement] as const);
    const list = h("div", { class: "check-list" }, ...boxes.map(([n, box]) => h("label", { class: "check" }, box, n)));
    return [list, () => boxes.filter(([, b]) => b.checked).map(([n]) => n)];
  };
  const [bladeList, chosenBlades] = picks(blades.map((b) => b.name), "Blade");
  const [tangList, chosenTangs] = picks(tangs.map((t) => t.name), "Tang");
  const replace = h("input", { type: "checkbox", "aria-label": "Replace files already in the pack" }) as HTMLInputElement;
  append(
    body,
    h("h3", {}, "Blades"),
    blades.length ? bladeList : h("p", { class: "hint" }, "No blades"),
    h("h3", {}, "Tangs"),
    tangs.length ? tangList : h("p", { class: "hint" }, "No tangs"),
    h("label", { class: "check" }, replace, "Replace existing files"),
  );
  foot.append(
    button("Cancel", close, { class: "ghost" }),
    button("Add", () => {
      void api<{ written: string[]; kept: string[] }>("/packs/add", "POST", { pack: pack.root, blades: chosenBlades(), tangs: chosenTangs(), replace: replace.checked }).then(
        (answer) => {
          close();
          toast(`Added ${answer.written.length} files to ${pack.name}${answer.kept.length ? `; kept ${answer.kept.length} already there` : ""}.`);
          onDone();
        },
        (error: Error) => toast(error.message, "error"),
      );
    }, { class: "primary", icon: "plus" }),
  );
}

function newMarketplace(onDone: () => void): void {
  const { body, foot, close } = openDialog("New marketplace repository");
  const field = (label: string, input: HTMLElement): HTMLElement => h("label", { class: "field" }, h("span", {}, label), input);
  const folder = h("input", { type: "text", "aria-label": "New folder", placeholder: "C:\\Users\\me\\repos\\data-blades", spellcheck: "false" });
  const marketplace = h("input", { type: "text", "aria-label": "Marketplace name", placeholder: "data-blades", spellcheck: "false" });
  const pack = h("input", { type: "text", "aria-label": "First pack's name", placeholder: "sales", spellcheck: "false" });
  const owner = h("input", { type: "text", "aria-label": "Owner" });
  const description = h("input", { type: "text", "aria-label": "Description" });
  append(
    body,
    field("New folder", folder),
    field("Marketplace name", marketplace),
    field("First pack's name", pack),
    field("Owner", owner),
    field("Description", description),
  );
  foot.append(
    button("Cancel", close, { class: "ghost" }),
    button("Create", () => {
      void api<{ files: string[] }>("/packs/new", "POST", {
        folder: folder.value.trim(),
        marketplace: marketplace.value.trim(),
        pack: pack.value.trim(),
        owner: owner.value.trim(),
        description: description.value.trim(),
        add_folder: true,
      }).then(
        (answer) => {
          close();
          toast(`Created ${answer.files.length} files.`);
          onDone();
        },
        (error: Error) => toast(error.message, "error"),
      );
    }, { class: "primary", icon: "plus" }),
  );
  folder.focus();
}
