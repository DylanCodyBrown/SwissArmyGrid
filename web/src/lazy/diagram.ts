/**
 * lazy/diagram.ts: Mermaid, loaded only when a diagram card (an ERD) first draws. [FUNCTION]
 *
 * securityLevel "strict" keeps Mermaid from running any HTML or script a label might carry; the
 * text comes from the server's ERD (column and table names) or an agent's diagram card.
 *
 * Nothing in a diagram may make the page fetch anything (the page sends nothing anywhere). Mermaid's
 * sanitizer kept <img src> in an HTML label, and an %%{init}%% directive could set themeCSS with
 * url(...): either one made the browser request a URL an agent chose, during render itself
 * (Mermaid measures the drawing in the page). So labels are SVG text, the style settings cannot be
 * changed from a diagram, and the drawing keeps no link or url() that leaves it.
 */
import mermaid from "mermaid";
import { contained, fitToCard } from "./svgSafe";

let ready: "light" | "dark" | null = null;
let count = 0;

// Settings a diagram's own %%{init}%% may not change (Mermaid's defaults, and the style ones).
const SECURE = ["secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges", "htmlLabels", "flowchart", "themeCSS", "themeVariables", "fontFamily", "theme", "darkMode", "er", "dompurifyConfig"];
/** Draw Mermaid text into `host` as SVG; on a syntax error, show the error instead. */
export async function drawDiagram(host: HTMLElement, text: string, dark: boolean): Promise<void> {
  const theme = dark ? "dark" : "light";
  if (ready !== theme) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      secure: SECURE,
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      theme: dark ? "dark" : "default",
      er: { useMaxWidth: false },
    });
    ready = theme;
  }
  count += 1;
  const id = `sag-diagram-${count}`;
  try {
    const { svg } = await mermaid.render(id, text);
    const drawing = contained(svg);
    if (drawing instanceof Element) fitToCard(drawing);
    host.replaceChildren(drawing);
  } catch (e) {
    // On a syntax error Mermaid leaves its "Syntax error in text" picture in the page body,
    // under everything else: take it away, the card says what went wrong.
    for (const left of document.querySelectorAll(`#${id}, #d${id}`)) if (!host.contains(left)) left.remove();
    host.textContent = `This diagram could not be drawn: ${(e as Error).message}`;
    host.classList.add("error-text");
  }
}
