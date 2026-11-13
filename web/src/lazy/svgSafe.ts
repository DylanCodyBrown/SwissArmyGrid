/**
 * lazy/svgSafe.ts: an SVG drawing made safe to put in the page. [FUNCTION]
 *
 * Shared by the Mermaid and Graphviz cards, whose text can come from an agent. The page sends
 * nothing anywhere and runs nothing a drawing carries: scripts, event handlers, animations that
 * could set a link, embedded HTML and every link or url() that leaves the drawing are taken out.
 */
const OUTSIDE_URL = /url\(\s*(?!['"]?#)[^)]*\)/gi; // url(...) that is not a reference inside the drawing
const DROPPED = "img, image, script, iframe, object, embed, audio, video, foreignObject, animate, set, animateMotion, animateTransform";

/** The drawing as a node to insert, with every link and url() that leaves it taken out. */
export function contained(svg: string): Node {
  // A parsed SVG document is inert: nothing in it loads until it is in the page.
  const drawing = new DOMParser().parseFromString(svg, "image/svg+xml");
  for (const element of drawing.querySelectorAll(DROPPED)) element.remove();
  for (const style of drawing.querySelectorAll("style")) style.textContent = (style.textContent ?? "").replace(/@import[^;]*;?/gi, "").replace(OUTSIDE_URL, "none");
  for (const element of drawing.querySelectorAll("*")) {
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase();
      const outside = /(^|:)(href|src|srcset|action|formaction)$/.test(name) && !attribute.value.trim().startsWith("#");
      if (outside || name.startsWith("on")) element.removeAttribute(attribute.name);
      else if (OUTSIDE_URL.test(attribute.value)) element.setAttribute(attribute.name, attribute.value.replace(OUTSIDE_URL, "none"));
      OUTSIDE_URL.lastIndex = 0;
    }
  }
  return document.importNode(drawing.documentElement, true);
}

/** The theme's chart colours (grid.css --cat-1 .. --cat-8), for figures that set none of their own. */
export function themePalette(): string[] {
  const style = getComputedStyle(document.documentElement);
  return [1, 2, 3, 4, 5, 6, 7, 8].map((i) => style.getPropertyValue(`--cat-${i}`).trim()).filter(Boolean);
}

/** Let the drawing shrink with its card, keeping its shape, but never grow past its own size: a small
 * diagram blown up to a wide card had 50px text. */
export function fitToCard(svg: Element): void {
  const box = (svg.getAttribute("viewBox") ?? "").split(/[\s,]+/).map(Number);
  const width = parseFloat(svg.getAttribute("width") ?? "") || box[2] || 0;
  const height = parseFloat(svg.getAttribute("height") ?? "") || box[3] || 0;
  if (!svg.getAttribute("viewBox") && width && height) svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.removeAttribute("width");
  svg.removeAttribute("height");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.classList.add("fit-svg");
  if (width && height && svg instanceof SVGElement) {
    svg.style.maxWidth = `${width}px`;
    svg.style.maxHeight = `${height}px`;
  }
}

