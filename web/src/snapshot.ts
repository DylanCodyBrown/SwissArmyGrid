/**
 * snapshot.ts: draws this page to a PNG when an agent asks to see it. [WIRE]
 *
 * An agent that wants to look at the screen (a chart it just added, how the steps read, whether a
 * split is lined up) sends a snapshot request to the server. The server relays it to every open page
 * as a `snapshot_request` notice. The page that is on screen draws the area asked for to a PNG with
 * html-to-image (it copies the element's DOM and computed styles into an SVG, then onto a canvas) and
 * uploads it; the server hands the bytes to the agent. It is the page as you see it: your scroll, your
 * selection, the open panels. Nothing is saved anywhere.
 *
 * A hidden tab does not answer (its layout is not real, and another visible page can), and a page
 * that cannot draw says why instead of leaving the agent waiting.
 *
 * ROUTING: [SNAPSHOT REQUEST]  snapshot_request notice -> answer(notice)
 *          [PICTURE]           -> POST /snapshots/{id}?width=&height=   (image/png)
 *          [FAILURE]           -> POST /snapshots/{id}/error            (the reason, as text)
 */
import { toBlob } from "html-to-image";
import { onNotice, token, type Notice } from "./api";

/** The areas an agent may ask for, and the element each one is. Keep in step with AREAS in
 * server/routes_snapshots.py. */
const AREAS: Record<string, string> = {
  app: "body",
  ribbon: ".ribbon",
  grid: ".panes",
  steps: ".steps-strip", // the panel opens in its own window; the grid page shows the strip
  sandbox: "#ai-panel",
};

/** Register the listener. Called once per page, beside the other live-notice handlers. */
export function mountSnapshots(): void {
  onNotice((notice: Notice) => {
    if (notice.type === "snapshot_request" && notice.id) void answer(notice.id, notice.area ?? "app", notice.scale ?? 1);
  });
}

async function answer(id: string, area: string, scale: number): Promise<void> {
  // A page that is not showing cannot draw itself truthfully, and a visible one will answer.
  if (document.visibilityState !== "visible") return;
  try {
    const selector = AREAS[area];
    const element = selector ? document.querySelector<HTMLElement>(selector) : null;
    if (!element) throw new Error(`There is no "${area}" area on this page.`);
    const box = element.getBoundingClientRect();
    // The whole app is the viewport, not the (possibly taller) body.
    const width = Math.round(area === "app" ? window.innerWidth : box.width);
    const height = Math.round(area === "app" ? window.innerHeight : box.height);
    if (width < 2 || height < 2) throw new Error(`The "${area}" area is not showing right now.`);
    const blob = await toBlob(element, {
      width,
      height,
      pixelRatio: scale,
      cacheBust: false,
      // The page's own background, so a transparent area does not come out black or white by accident.
      backgroundColor: getComputedStyle(document.body).backgroundColor,
    });
    if (!blob) throw new Error("The browser returned no picture.");
    await send(`/snapshots/${encodeURIComponent(id)}?width=${width * scale}&height=${height * scale}`, blob, "image/png");
  } catch (e) {
    await send(`/snapshots/${encodeURIComponent(id)}/error`, (e as Error).message || "unknown error", "text/plain");
  }
}

/** One POST with the page's token. The answer is best effort: if another page answered first, the
 * server says 404 and there is nothing to do about it. */
async function send(path: string, body: Blob | string, type: string): Promise<void> {
  await fetch(path, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": type },
    body,
  }).catch(() => undefined);
}
