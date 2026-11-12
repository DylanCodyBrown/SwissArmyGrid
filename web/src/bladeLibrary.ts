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
  body.append(
    h(
      "p",
      { class: "hint" },
      "Check each blade's code before you import it: it runs on this computer whenever the blade is used. Importing runs nothing.",
    ),
  );
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
            ["skip", "Skip this one"],
          ]) {
            select.append(h("option", { value }, label));
          }
          select.addEventListener("change", () => (choices[String(item.index)] = select.value));
          return h("label", { class: "warning-text" }, `A blade named ${item.conflict.name} already exists: `, select);
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
  const where = h("select", { "aria-label": "Save the blades in" }, h("option", { value: "" }, "This app (on this computer)"));
  try {
    for (const folder of (await bladeFolders()).folders) if (folder.exists) where.append(h("option", { value: folder.path }, `Blade folder: ${folder.path}`));
  } catch {
    // no folders to offer
  }
  const importable = items.filter((i) => i.blade).length;
  body.append(h("label", { class: "field" }, h("span", {}, "Save them in"), where));
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
  const waiting = settings.filter((s) => s.placeholder).length;
  return h(
    "fieldset",
    { class: "fill-in" },
    h("legend", {}, "Fill in your values"),
    h(
      "p",
      { class: "hint" },
      waiting
        ? `${waiting} of these ${waiting === 1 ? "is an example value" : "are example values"} from whoever wrote the blade. Put in your own; they are written into its code when you import.`
        : "The values its code is written with. Change any that differ for you; they are written into its code when you import.",
    ),
    ...settings.map((setting) =>
      settingField(setting, (value) => {
        if (value === String(setting.value)) delete mine.settings[setting.id];
        else mine.settings[setting.id] = value;
      }),
    ),
    ...secrets.map((secret) => {
      const input = h("input", { type: "password", autocomplete: "new-password", placeholder: "optional: type it now or later", "aria-label": `secret ${secret}` });
      input.addEventListener("input", () => (mine.secrets[secret] = input.value));
      return h(
        "div",
        { class: "setting-row" },
        h("label", {}, h("span", { class: "setting-key" }, `secret("${secret}")`), h("span", { class: "hint" }, " kept in this computer's vault, never in the file")),
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
          h("span", { class: "hint" }, folder.exists ? `  ${folder.blades ?? 0} blades` : "  not found"),
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
    h("p", { class: "hint" }, "SwissArmyGrid looks in these folders (and up to four levels below) for Python files that start with a blade header. Ordinary Python files are left alone; so are virtual environments and hidden folders."),
    list,
    h("div", { class: "row-actions" }, input, button("Add", () => {
      const path = input.value.trim();
      if (path && !mine.includes(path)) mine.push(path);
      input.value = "";
      render();
    }, { icon: "plus" })),
    state.problems.length
      ? h("div", {}, h("h3", {}, "Files with a blade header that could not be read"), h("ul", {}, ...state.problems.map((p) => h("li", {}, h("code", {}, p.path), h("div", { class: "status-error" }, p.error)))))
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
