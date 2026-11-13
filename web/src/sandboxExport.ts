/**
 * sandboxExport.ts: the whole Sandbox as one markdown file (Plans/sandbox-report.plan.md, S3). [SHARED]
 *
 * Each card in its order becomes a section: its title, where it came from, then a note's own markdown
 * (its headings moved down under the section's), a table card's rows as a markdown table, and every
 * chart, figure, diagram and graph as a PNG embedded in the file (![title](data:image/png;base64,...)),
 * so one file carries everything: ready to read, to keep, or to hand to an AI model.
 */
import { mdTable } from "./analysisCards";

export interface ExportCard {
  id: string;
  kind: string;
  title: string;
  dataset: string | null;
  origin?: string;
  body?: string;
  columns?: string[];
  rows?: unknown[][];
  error?: string;
}

/** A note's headings moved two levels down, so they sit under its section's title. */
function demote(markdown: string): string {
  return markdown.replace(/^(#{1,4})\s/gm, (_, hashes: string) => `${"#".repeat(Math.min(6, hashes.length + 2))} `);
}

export async function sandboxMarkdown(cards: ExportCard[], picture: (id: string) => Promise<string | null>, when: Date): Promise<string> {
  const from = (c: ExportCard): string => (c.dataset ? `the grid \`${c.dataset}\`` : `${c.origin ?? "the Sandbox"}`);
  const tables = [...new Set(cards.map((c) => c.dataset ?? c.origin ?? "Sandbox"))];
  const out = [
    "# Sandbox",
    "",
    `Exported from SwissArmyGrid on ${when.toLocaleString()}. ${cards.length} card${cards.length === 1 ? "" : "s"}, from ${tables.map((t) => `\`${t}\``).join(", ")}.`,
    "Each section below is one card, in the order they were arranged; charts are embedded as PNG images.",
  ];
  for (const card of cards) {
    const title = card.title || card.kind;
    out.push("", "---", "", `## ${title}`, "", `*From ${from(card)} (${card.kind}).*`, "");
    if (card.error) {
      out.push(`> This card could not be drawn: ${card.error}`);
    } else if (card.kind === "note") {
      out.push(demote(card.body ?? ""));
    } else if (card.kind === "table" && card.columns) {
      out.push(mdTable(card.columns, card.rows ?? []));
    } else {
      const image = await picture(card.id).catch(() => null);
      out.push(image ? `![${title.replace(/[[\]]/g, "")}](${image})` : "> (This drawing was not on screen to capture; open the Sandbox and export again.)");
    }
  }
  return `${out.join("\n")}\n`;
}

/** A drawn SVG (a diagram, a graph) as a PNG data URL at twice its size, on white. A drawing whose
 * labels are HTML (Mermaid's) makes the canvas unreadable to the page; then the SVG itself comes back,
 * as a data URL, which markdown viewers show just the same. */
export async function svgToPng(svg: SVGSVGElement): Promise<string> {
  const box = svg.getBoundingClientRect();
  const width = Math.max(1, Math.round(box.width)) || 600;
  const height = Math.max(1, Math.round(box.height)) || 400;
  const copy = svg.cloneNode(true) as SVGSVGElement;
  copy.setAttribute("width", String(width));
  copy.setAttribute("height", String(height));
  copy.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const text = new XMLSerializer().serializeToString(copy);
  const svgUrl = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(text)))}`;
  const image = new Image();
  image.src = svgUrl;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = width * 2;
  canvas.height = height * 2;
  const context = canvas.getContext("2d");
  if (!context) return svgUrl;
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  try {
    return canvas.toDataURL("image/png");
  } catch {
    return svgUrl;
  }
}
