/**
 * theme.ts: the four themes, following the system, and zoom. [STATE + FUNCTION]
 *
 * The effective theme lands on html[data-theme] (light, dark, hc-light, hc-dark); grid.css
 * defines every color as a token per theme. "System" follows prefers-color-scheme and
 * prefers-contrast: more, and re-resolves when either changes. AG Grid gets one theme object
 * whose colors are those same CSS variables, so a theme switch needs no grid rebuild; only zoom
 * (font size and row height are numbers) rebuilds the theme object.
 */
import { themeQuartz, type Theme } from "ag-grid-community";
import { recall, remember } from "./dom";

export type ThemeChoice = "system" | "light" | "dark" | "hc-light" | "hc-dark";
export const THEME_LABELS: Record<ThemeChoice, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
  "hc-light": "High contrast light",
  "hc-dark": "High contrast dark",
};

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const contrastQuery = matchMedia("(prefers-contrast: more)");
let choice: ThemeChoice = (recall("theme") as ThemeChoice | null) ?? "system";
const listeners: (() => void)[] = [];

export function effectiveTheme(): Exclude<ThemeChoice, "system"> {
  if (choice !== "system") return choice;
  const dark = darkQuery.matches;
  if (contrastQuery.matches) return dark ? "hc-dark" : "hc-light";
  return dark ? "dark" : "light";
}

export function themeChoice(): ThemeChoice {
  return choice;
}

export function setTheme(next: ThemeChoice): void {
  choice = next;
  remember("theme", next);
  apply();
}

/** Subscribe to theme and zoom changes; returns the unsubscribe (a grid view calls it when it closes). */
export function onThemeChange(listener: () => void): () => void {
  listeners.push(listener);
  return () => removeListener(listeners, listener);
}

function removeListener(list: (() => void)[], listener: () => void): void {
  const at = list.indexOf(listener);
  if (at >= 0) list.splice(at, 1);
}

function apply(): void {
  document.documentElement.dataset.theme = effectiveTheme();
  for (const listener of [...listeners]) listener();
}

darkQuery.addEventListener("change", apply);
contrastQuery.addEventListener("change", apply);
// Another window or workspace tab changed the theme (browsers tell every other page of this
// origin): follow it, so every tab and pop-out matches.
window.addEventListener("storage", (event) => {
  if (event.key !== "swissarmygrid.theme" || !event.newValue || !(event.newValue in THEME_LABELS)) return;
  choice = event.newValue as ThemeChoice;
  apply();
});
apply();

// ===== column profiles in the headers (off unless turned on in Settings) =====

let profiles = recall("headerProfiles") === "1";
const profileListeners: (() => void)[] = [];

/** Whether each column header shows its distinct count, blank share and a small histogram. */
export function headerProfiles(): boolean {
  return profiles;
}

export function setHeaderProfiles(on: boolean): void {
  profiles = on;
  remember("headerProfiles", on ? "1" : "0");
  for (const listener of profileListeners) listener();
}

export function onHeaderProfilesChange(listener: () => void): () => void {
  profileListeners.push(listener);
  return () => removeListener(profileListeners, listener);
}

window.addEventListener("storage", (event) => {
  if (event.key !== "swissarmygrid.headerProfiles") return;
  profiles = event.newValue === "1";
  for (const listener of profileListeners) listener();
});

// ===== zoom =====

export const ZOOM_STEPS = [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6, 1.8, 2];
let zoom = Number(recall("zoom") ?? "1") || 1;

export function gridZoom(): number {
  return zoom;
}

export function setGridZoom(next: number): void {
  zoom = Math.min(2, Math.max(0.7, Math.round(next * 100) / 100));
  remember("zoom", String(zoom));
  document.documentElement.style.setProperty("--ui-zoom", String(zoom));
  for (const listener of listeners) listener();
}

export function stepZoom(direction: 1 | -1): void {
  const index = ZOOM_STEPS.findIndex((z) => z >= zoom - 0.001);
  const at = index === -1 ? ZOOM_STEPS.length - 1 : index;
  setGridZoom(ZOOM_STEPS[Math.min(ZOOM_STEPS.length - 1, Math.max(0, at + direction))] ?? 1);
}

document.documentElement.style.setProperty("--ui-zoom", String(zoom));

/** The grid's row height in pixels at this zoom. Given to AG Grid as its `rowHeight` option as well as in
 * the theme: measured from the theme's CSS alone, AG Grid can read its own 42px default before that CSS
 * is in place and keep it for every row after the first (the first row 32px, the rest 42px, a gap
 * between them). */
export function gridRowHeight(): number {
  return Math.round(32 * zoom);
}

/** The AG Grid theme: colors from the CSS tokens, sizes from the zoom level. No zebra
 * striping: rows are told apart by borders, not by alternating color (plan section 13). */
export function gridTheme(): Theme {
  const hc = effectiveTheme().startsWith("hc");
  return themeQuartz.withParams({
    backgroundColor: "var(--bg)",
    foregroundColor: "var(--fg)",
    headerBackgroundColor: "var(--surface)",
    headerTextColor: "var(--fg)",
    borderColor: hc ? "var(--border-strong)" : "var(--border)",
    accentColor: "var(--accent)",
    selectedRowBackgroundColor: "var(--selected)",
    rowHoverColor: "var(--row-hover)",
    oddRowBackgroundColor: "var(--bg)",
    chromeBackgroundColor: "var(--surface)",
    fontFamily: '"Segoe UI", system-ui, sans-serif',
    fontSize: Math.round(13 * zoom),
    headerFontSize: Math.round(13 * zoom),
    rowHeight: gridRowHeight(),
    // Taller only when the headers carry their column profile (two more lines).
    headerHeight: Math.round((headerProfiles() ? 52 : 34) * zoom),
    spacing: Math.round(6 * zoom),
    wrapperBorderRadius: 0,
    rowBorder: { width: hc ? 2 : 1, color: hc ? "var(--border-strong)" : "var(--border)" },
    checkboxUncheckedBorderColor: "var(--border-strong)",
    columnBorder: hc,
  });
}
