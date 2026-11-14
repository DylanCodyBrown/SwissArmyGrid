/**
 * fileChooser.ts: pick a file or folder on this computer by its real path (a browser's own picker
 * hides the path). Shared by the Connections form's file and folder fields and the Workbench's Open
 * and Save. [WIRE]
 *
 * ROUTING: [LIST] -> GET /folders?path=&suffixes=  (names, sizes and dates; the user's alone)
 *
 *   [PATH]    the folder shown, typed or pasted (Enter goes there); Up; the home folder and drives
 *   [LIST]    its folders, then its files of the wanted kinds; a folder opens on click, a file is picked
 *   [SAVE]    a file name box, for Save as: the folder shown plus that name
 */
import { api } from "./api";
import { button, clear, formatCount, h, openDialog } from "./dom";

interface Listing {
  path: string;
  parent: string | null;
  folders: { name: string; path: string }[];
  files: { name: string; path: string; size: number; modified: number }[];
  roots: string[];
  home: string;
}

export interface ChooseOptions {
  title: string;
  /** "file" picks a file, "folder" the folder shown, "save" a new or existing file's path. */
  mode: "file" | "folder" | "save";
  /** File kinds to show, as [".csv", ".xlsx"]; none: every file. */
  suffixes?: string[];
  /** Where to start: a file's path starts in its folder. */
  start?: string;
  /** Save: the name to start with. */
  name?: string;
}

const size = (bytes: number): string =>
  bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${formatCount(Math.round(bytes / 1024))} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;

/** The chosen path, or null when the window is closed without one. */
export function chooseFile(options: ChooseOptions): Promise<string | null> {
  return new Promise((resolve) => {
    const { dialog, body, foot, close } = openDialog(options.title, true);
    dialog.classList.add("file-chooser");
    let done = false;
    const finish = (value: string | null): void => {
      if (done) return;
      done = true;
      close();
      resolve(value);
    };
    dialog.addEventListener("close", () => finish(null));

    let shown: Listing | null = null;
    const where = h("input", { type: "text", class: "mono", "aria-label": "Folder", spellcheck: false });
    const roots = h("select", { "aria-label": "Start from" });
    const list = h("ul", { class: "file-list", role: "listbox", "aria-label": "Folders and files" });
    const status = h("p", { class: "hint", role: "status" });
    const saveName = h("input", { type: "text", "aria-label": "File name", value: options.name ?? "", spellcheck: false });
    const choose = button(options.mode === "folder" ? "Choose this folder" : options.mode === "save" ? "Save here" : "Choose", () => pick(), { class: "primary", icon: "check" });

    const pick = (): void => {
      if (!shown) return;
      if (options.mode === "folder") finish(shown.path);
      else if (options.mode === "save") {
        const name = saveName.value.trim();
        if (!name || /[\\/]/.test(name)) {
          status.textContent = "Type a file name (no slashes)";
          return;
        }
        const sep = shown.path.includes("\\") && !shown.path.includes("/") ? "\\" : "/";
        finish(`${shown.path.replace(/[\\/]+$/, "")}${sep}${name}`);
      }
    };

    // Only the newest listing is drawn: a path typed while the first folder is still being read would
    // otherwise be replaced by that first answer when it arrives late.
    let asked = 0;
    const go = async (path: string | null): Promise<void> => {
      const query = new URLSearchParams();
      if (path) query.set("path", path);
      if (options.suffixes?.length) query.set("suffixes", options.suffixes.join(","));
      status.textContent = "Loading…";
      const mine = ++asked;
      let found: Listing;
      try {
        found = await api<Listing>(`/folders?${query}`);
      } catch (e) {
        if (mine === asked) status.textContent = (e as Error).message;
        return;
      }
      if (mine !== asked) return;
      shown = found;
      const at = found;
      where.value = at.path;
      clear(roots, ...at.roots.map((r) => h("option", { value: r, selected: r === at.path }, r === at.home ? `Home (${r})` : r)));
      const rows = [
        ...(at.parent ? [h("li", { class: "file-row folder", role: "option", tabindex: 0, onclick: () => void go(at.parent) }, h("span", { class: "file-name" }, ".."))] : []),
        ...at.folders.map((f) => h("li", { class: "file-row folder", role: "option", tabindex: 0, title: f.path, onclick: () => void go(f.path) }, h("span", { class: "file-name" }, `${f.name}/`))),
        ...at.files.map((f) =>
          h(
            "li",
            {
              class: `file-row file${options.mode === "folder" ? " muted" : ""}`,
              role: "option",
              tabindex: 0,
              title: f.path,
              onclick: () => {
                if (options.mode === "file") finish(f.path);
                else if (options.mode === "save") saveName.value = f.name;
              },
            },
            h("span", { class: "file-name" }, f.name),
            h("span", { class: "file-meta" }, `${size(f.size)} · ${new Date(f.modified * 1000).toLocaleDateString()}`),
          ),
        ),
      ];
      for (const row of rows) row.addEventListener("keydown", (e) => { if (e.key === "Enter") row.click(); });
      clear(list, ...rows);
      const kinds = options.suffixes?.length ? ` (${options.suffixes.join(", ")})` : "";
      status.textContent = `${at.folders.length} folder${at.folders.length === 1 ? "" : "s"}, ${at.files.length} file${at.files.length === 1 ? "" : "s"}${kinds}`;
    };

    where.addEventListener("keydown", (e) => { if (e.key === "Enter") void go(where.value.trim()); });
    roots.addEventListener("change", () => void go(roots.value));
    body.append(
      h("div", { class: "file-path" }, roots, where, button("Go", () => void go(where.value.trim()), { icon: "play" })),
      list,
      ...(options.mode === "save" ? [h("label", { class: "field file-save" }, h("span", {}, "File name"), saveName)] : []),
      status,
    );
    foot.append(...(options.mode === "file" ? [] : [choose]), button("Cancel", () => finish(null)));
    void go(options.start ?? null);
  });
}
