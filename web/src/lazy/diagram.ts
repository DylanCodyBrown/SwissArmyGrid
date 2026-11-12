/**
 * lazy/diagram.ts: Mermaid, loaded only when a diagram card (an ERD) first draws. [FUNCTION]
 *
 * securityLevel "strict" keeps Mermaid from running any HTML or script a label might carry; the
 * text comes from the server's ERD (column and table names) or an agent's diagram card.
 */
import mermaid from "mermaid";

let ready: "light" | "dark" | null = null;
let count = 0;

/** Draw Mermaid text into `host` as SVG; on a syntax error, show the error instead. */
export async function drawDiagram(host: HTMLElement, text: string, dark: boolean): Promise<void> {
  const theme = dark ? "dark" : "light";
  if (ready !== theme) {
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default", er: { useMaxWidth: false } });
    ready = theme;
  }
  count += 1;
  const id = `sag-diagram-${count}`;
  try {
    const { svg } = await mermaid.render(id, text);
    host.innerHTML = svg;
  } catch (e) {
    // On a syntax error Mermaid leaves its "Syntax error in text" picture in the page body,
    // under everything else: take it away, the card says what went wrong.
    for (const left of document.querySelectorAll(`#${id}, #d${id}`)) if (!host.contains(left)) left.remove();
    host.textContent = `This diagram could not be drawn: ${(e as Error).message}`;
    host.classList.add("error-text");
  }
}
