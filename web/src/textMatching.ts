/**
 * textMatching.ts: how this table's filters and rules compare text. [WIRE + EXPORT]
 *
 * A table-level setting, so it sits on the ribbon's Table row. It is saved with the grid's steps
 * on the server, so the filters, the categorize rules and the exported Python all compare text
 * the same way (and an agent sees the same setting).
 *
 * ROUTING: [MATCHING BUTTON]  -> openTextMatching()  (its label says what is on)
 *          [MATCH CASE / WILDCARDS / TRIM / EMPTY = BLANK] -> toggleSetting(key)  (POST steps + settings)
 */
import type { Settings } from "./api";
import { button, h, openDialog, toast } from "./dom";
import { state } from "./state";

const MATCHING: [keyof Settings, string, string][] = [
  ["case_sensitive", "Match case", "Oslo and oslo are different values"],
  ["wildcards", "Wildcards", "% matches any run of characters in equals filters, as in SQL: Acme% or %Corp"],
  ["trim_spaces", "Ignore spaces at the ends", "' Rome ' matches 'Rome'"],
  ["empty_is_blank", "Empty text is blank", "'' counts as a blank value"],
];

/** What is on, in a few words: "case, trim", or "ignore case" when nothing is. */
export function matchingSummary(): string {
  const s = state.info?.settings;
  const short: Record<keyof Settings, string> = { case_sensitive: "case", wildcards: "wildcards", trim_spaces: "trim", empty_is_blank: "empty = blank" };
  const on = s ? MATCHING.filter(([key]) => s[key]).map(([key]) => short[key]) : [];
  return on.length ? on.join(", ") : "ignore case";
}

export function openTextMatching(): void {
  const info = state.info;
  if (!info) return;
  const { body, foot, close } = openDialog("Text matching");
  const boxes = MATCHING.map(([key, label, example]) => {
    const box = h("input", { type: "checkbox", checked: info.settings[key], onchange: () => void toggleSetting(key, boxes) });
    return { key, element: h("label", { class: "choice" }, box, h("span", {}, h("strong", {}, label), h("small", {}, example))), box };
  });
  body.append(
    h("p", { class: "hint" }, "How this table's filters and categorize rules compare text. Saved with its steps, so the exported Python matches the same way."),
    ...boxes.map((b) => b.element),
  );
  foot.append(button("Close", close, { class: "primary" }));
  boxes[0]?.box.focus();
}

async function toggleSetting(key: keyof Settings, boxes: { key: keyof Settings; box: HTMLInputElement }[]): Promise<void> {
  const info = state.info!;
  try {
    await state.saveSteps(info.steps, { ...info.settings, [key]: !info.settings[key] });
  } catch (e) {
    toast((e as Error).message, "error");
  }
  for (const b of boxes) b.box.checked = state.info!.settings[b.key];
}
