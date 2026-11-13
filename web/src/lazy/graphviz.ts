/**
 * lazy/graphviz.ts: Graphviz (DOT) graphs, loaded only when a Graphviz card first draws. [FUNCTION]
 *
 * Graphviz runs in the page as WebAssembly (@viz-js/viz): no file system, no network. Its SVG goes
 * through svgSafe, so a URL or href in the DOT text is dropped rather than followed.
 */
import { instance, type Viz } from "@viz-js/viz";
import { contained, fitToCard } from "./svgSafe";

let viz: Promise<Viz> | null = null;

/** Draw DOT text into `host`; on an error, say what went wrong instead. */
export async function drawGraphviz(host: HTMLElement, dot: string, dark: boolean): Promise<void> {
  viz ??= instance();
  const engine = await viz;
  const ink = dark ? "#e8ecef" : "#1c2228";
  const result = engine.render(dot, {
    format: "svg",
    // The theme's ink for anything the graph does not colour itself; a transparent page.
    // and the page's sans-serif in place of Graphviz's Times.
    graphAttributes: { bgcolor: "transparent", fontcolor: ink, color: ink, fontname: "Arial" },
    nodeAttributes: { fontcolor: ink, color: ink, fontname: "Arial" },
    edgeAttributes: { fontcolor: ink, color: ink, fontname: "Arial", fontsize: 11 },
  });
  if (result.status !== "success") {
    host.textContent = `This graph could not be drawn: ${result.errors.map((e) => e.message).join(" ") || "the DOT text is not valid."}`;
    host.classList.add("error-text");
    return;
  }
  const drawing = contained(result.output);
  if (drawing instanceof Element) fitToCard(drawing);
  host.replaceChildren(drawing);
}
