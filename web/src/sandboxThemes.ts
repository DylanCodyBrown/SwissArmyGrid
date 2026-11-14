/**
 * sandboxThemes.ts: the Sandbox's own theme (server: domain/themes.py, GET/PUT /sandbox/theme).
 *
 * A theme is a set of the page's colour tokens (--bg, --fg, --accent, --cat-1..8, ...). They are set on
 * the Sandbox's element only, so its cards, charts, Page view, PDF and markdown pictures take the theme
 * while the grid keeps the app's. "Follow the app" removes them again. Every built-in theme (and every
 * imported one, once the server has made it readable) meets its contrast.
 */
import { api } from "./api";
import { button, h, toast } from "./dom";
import { saveBlob } from "./profileView";
import { effectiveTheme } from "./theme";

export interface SandboxTheme {
  id: string;
  label: string;
  dark: boolean;
  group: string;
  note: string;
  custom: boolean;
  by_agent: boolean;
  tokens: Record<string, string>;
  cat: string[];
}

interface Themes {
  current: string;
  theme: SandboxTheme | null;
  themes: SandboxTheme[];
}

let current: SandboxTheme | null = null;
let applied: string[] = [];

export function sandboxTheme(): SandboxTheme | null {
  return current;
}

/** Whether the Sandbox draws on a dark ground: its theme's, or the app's when it follows the app. */
export function sandboxDark(): boolean {
  return current ? current.dark : effectiveTheme() !== "light" && effectiveTheme() !== "hc-light";
}

/** Put the theme's tokens on `host` (and only there), or take them off for "Follow the app". */
export function applySandboxTheme(host: HTMLElement, theme: SandboxTheme | null): void {
  current = theme;
  for (const name of applied) host.style.removeProperty(name);
  applied = [];
  if (!theme) {
    delete host.dataset.sandboxTheme;
    host.style.removeProperty("color-scheme");
    return;
  }
  const set = (name: string, value: string): void => {
    host.style.setProperty(name, value);
    applied.push(name);
  };
  for (const [name, value] of Object.entries(theme.tokens)) set(`--${name}`, value);
  theme.cat.forEach((value, i) => set(`--cat-${i + 1}`, value));
  host.dataset.sandboxTheme = theme.id;
  host.style.setProperty("color-scheme", theme.dark ? "dark" : "light");
}

export async function loadThemes(): Promise<Themes> {
  return api<Themes>("/sandbox/themes");
}

/** The theme picker, Import theme… and Export theme, for the Sandbox's toolbar. */
export function themeControls(themes: Themes, changed: () => void): HTMLElement {
  const groups = new Map<string, SandboxTheme[]>();
  for (const t of themes.themes) groups.set(t.group, [...(groups.get(t.group) ?? []), t]);
  const select = h(
    "select",
    { "aria-label": "Sandbox theme", title: "Sandbox theme" },
    h("option", { value: "app", selected: themes.current === "app" }, "Theme: follow the app"),
    ...[...groups.entries()].map(([group, list]) =>
      h("optgroup", { label: group }, ...list.map((t) => h("option", { value: t.id, selected: t.id === themes.current, title: t.note }, `${t.label}${t.by_agent ? " (agent)" : ""}`))),
    ),
  );
  select.addEventListener("change", () => {
    void api("/sandbox/theme", "PUT", { theme: select.value }).then(changed, (e: Error) => toast(e.message, "error"));
  });
  const file = h("input", { type: "file", accept: ".json,application/json", hidden: true, "aria-label": "Theme file" });
  file.addEventListener("change", () => {
    const picked = file.files?.[0];
    if (!picked) return;
    void picked.text().then(async (text) => {
      try {
        const added = await api<SandboxTheme>("/sandbox/themes", "POST", JSON.parse(text));
        await api("/sandbox/theme", "PUT", { theme: added.id });
        toast(`Imported theme ${added.label}.`);
        changed();
      } catch (e) {
        toast(e instanceof SyntaxError ? "That file is not JSON." : (e as Error).message, "error");
      }
    });
    file.value = "";
  });
  const chosen = themes.themes.find((t) => t.id === themes.current) ?? null;
  return h(
    "span",
    { class: "theme-controls" },
    select,
    button("Import theme…", () => file.click(), { icon: "import", iconOnly: true, class: "ghost" }),
    chosen
      ? button("Export theme", () => saveBlob(new Blob([JSON.stringify({ name: chosen.label, dark: chosen.dark, tokens: { ...chosen.tokens, cat: chosen.cat } }, null, 2)], { type: "application/json" }), `${chosen.id}.theme.json`), { icon: "export", iconOnly: true, class: "ghost" })
      : null,
    chosen?.by_agent ? button("Keep this theme", () => void api("/sandbox/themes", "POST", { name: chosen.label, dark: chosen.dark, tokens: { ...chosen.tokens, cat: chosen.cat } }).then(() => { toast(`Kept ${chosen.label}.`); changed(); }, (e: Error) => toast(e.message, "error")), { icon: "save", class: "ghost" }) : null,
    file,
  );
}
