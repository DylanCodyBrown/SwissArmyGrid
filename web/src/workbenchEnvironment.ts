/**
 * workbenchEnvironment.ts: New environment, a notebook's own Python (the Workbench's Python picker).
 * [WIRE]
 *
 *   [WHERE]      a folder that does not exist yet: .venv beside the document by default (Browse…)
 *   [PYTHON]     this app's own version, or 3.13 to 3.10 (uv fetches one this computer lacks)
 *   [LIBRARIES]  presets (Data analysis, Charts, Statistics and models, Databases) and every library
 *                by group, ticked; any other package typed; "add their imports" puts a first cell in
 *   [MAKE]       the steps' output as they run; done: the document runs in it from the next Run, and
 *                the Workbench remembers it for that document
 *
 * ROUTING: GET /workbench/environments; POST /workbench/environments (streamed, one JSON object a line)
 */
import { api, token } from "./api";
import { button, clear, h, openDialog } from "./dom";
import { chooseFile } from "./fileChooser";

interface Offer {
  libraries: { pip: string; label: string; group: string; imports: string; note: string }[];
  presets: Record<string, string[]>;
  versions: string[];
  always: string[];
}

export interface MadeEnvironment {
  python: string;
  imports: string[];
}

const join = (folder: string, name: string): string => `${folder.replace(/[\\/]+$/, "")}${folder.includes("\\") && !folder.includes("/") ? "\\" : "/"}${name}`;

/** The dialog; the new environment's Python (and the import lines chosen), or null when closed. */
export async function newEnvironment(start: { folder: string; document: string | null; appVersion: string }): Promise<MadeEnvironment | null> {
  const offer = await api<Offer>("/workbench/environments");
  return new Promise((resolve) => {
    const { dialog, body, foot, close } = openDialog("New environment", true);
    let made: MadeEnvironment | null = null;
    dialog.addEventListener("close", () => resolve(made));

    const where = h("input", { type: "text", class: "mono", value: join(start.folder, ".venv"), "aria-label": "Folder", spellcheck: false });
    const python = h("select", { "aria-label": "Python version" }, h("option", { value: "" }, `This app's own (${start.appVersion})`), ...offer.versions.map((v) => h("option", { value: v }, `Python ${v}`)));
    const boxes = new Map<string, HTMLInputElement>();
    const groups = [...new Set(offer.libraries.map((l) => l.group))];
    const presets = h(
      "div",
      { class: "env-presets", role: "group", "aria-label": "Presets" },
      ...Object.entries(offer.presets).map(([name, pips]) =>
        button(name, () => {
          for (const [pip, box] of boxes) box.checked = pips.includes(pip);
        }, { class: "chip" }),
      ),
      button("None", () => { for (const box of boxes.values()) box.checked = false; }, { class: "chip ghost" }),
    );
    const lists = h(
      "div",
      { class: "env-groups" },
      ...groups.map((group) =>
        h(
          "fieldset",
          { class: "env-group" },
          h("legend", {}, group),
          ...offer.libraries
            .filter((l) => l.group === group)
            .map((l) => {
              const box = h("input", { type: "checkbox", value: l.pip, checked: (offer.presets["Data analysis"] ?? []).includes(l.pip) }) as HTMLInputElement;
              boxes.set(l.pip, box);
              return h("label", { class: "check", title: l.imports || l.pip }, box, l.label, l.note ? h("span", { class: "hint" }, ` ${l.note}`) : null);
            }),
        ),
      ),
    );
    const others = h("input", { type: "text", "aria-label": "Other packages", placeholder: "Package names", spellcheck: false });
    const addImports = h("input", { type: "checkbox", checked: true }) as HTMLInputElement;
    const log = h("pre", { class: "env-log", hidden: true, "aria-live": "polite" });
    const status = h("p", { class: "hint", role: "status" }, `Always installed: ${offer.always.join(", ")}`);

    const make = async (): Promise<void> => {
      const packages = [...[...boxes.values()].filter((b) => b.checked).map((b) => b.value), ...others.value.split(/[\s,]+/).filter(Boolean)];
      const imports = offer.libraries.filter((l) => packages.includes(l.pip) && l.imports).map((l) => l.imports);
      create.disabled = true;
      log.hidden = false;
      clear(log);
      status.textContent = "Making…";
      try {
        const response = await fetch("/workbench/environments", {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ folder: where.value.trim(), python: python.value || null, packages, document: start.document }),
        });
        if (!response.ok || !response.body) throw new Error(await response.text());
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (value) pending += decoder.decode(value, { stream: true });
          let at: number;
          while ((at = pending.indexOf("\n")) >= 0) {
            const event = JSON.parse(pending.slice(0, at)) as { line?: string; done?: { ok: boolean; python?: string; error?: string } };
            pending = pending.slice(at + 1);
            if (event.line !== undefined) {
              log.textContent += event.line + "\n";
              log.scrollTop = log.scrollHeight;
            }
            if (event.done?.ok && event.done.python) {
              made = { python: event.done.python, imports: addImports.checked ? imports : [] };
              status.textContent = `Ready: ${event.done.python}`;
              clear(foot, button("Use it", close, { icon: "check", class: "primary" }));
              return;
            }
            if (event.done && !event.done.ok) throw new Error(event.done.error ?? "It could not be made.");
          }
          if (done) break;
        }
      } catch (e) {
        status.textContent = (e as Error).message;
        status.className = "status-error";
        create.disabled = false;
      }
    };

    const create = button("Make it", () => void make(), { icon: "play", class: "primary" });
    body.append(
      h("label", { class: "field" }, h("span", {}, "Folder"), h("span", { class: "field-row" }, where, button("Browse…", async () => {
        const folder = await chooseFile({ title: "Choose folder", mode: "folder", start: start.folder });
        if (folder) where.value = join(folder, ".venv");
      }, { icon: "folder" }))),
      h("label", { class: "field" }, h("span", {}, "Python"), python),
      h("div", { class: "field" }, h("span", {}, "Libraries"), presets, lists),
      h("label", { class: "field" }, h("span", {}, "Other packages"), others),
      h("label", { class: "check" }, addImports, "Add imports to the notebook"),
      status,
      log,
    );
    foot.append(create, button("Cancel", close));
  });
}
