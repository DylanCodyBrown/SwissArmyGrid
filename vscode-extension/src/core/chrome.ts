// chrome.ts -- "Open SAG in Chrome": find Google Chrome and start it on the workspace address. No vscode import, and the two
// things that touch the machine (does this file exist, start this program) are passed in, so the unit tests run without
// VS Code or Chrome and the integration tests can swap in a fake launcher.
//
// Why a button at all: the grids open inside VS Code's own browser tab by themselves, and that stays the default. Some
// people want the grid full size on another monitor, or in the browser they already use for everything else. Pressing
// the button is that choice. Nothing here runs unless the button does.
//
// The address carries the server's token in its #fragment, which a browser never sends over the network, exactly as the
// VS Code tab's address does. Chrome receives it on its command line, which other programs of the same user can read,
// as they can read the connection file the token comes from (~/.swissarmygrid/server.json): nothing new is exposed.

import type { ChildProcess, SpawnOptions } from "child_process";

export type Spawn = (command: string, args: string[], options: SpawnOptions) => ChildProcess;

/** What the launcher needs from the machine. */
export interface ChromeDeps {
  spawn: Spawn;
  exists: (path: string) => boolean;
}

export type Opened =
  | { ok: true; chrome: string }
  | { ok: false; reason: "not-found" | "failed"; message: string };

/** Where Chrome usually is, most likely first. A bare name (no folder) is looked up on PATH by the operating system. */
export function chromeCandidates(platform: NodeJS.Platform, env: Record<string, string | undefined>): string[] {
  if (platform === "win32") {
    const join = (root: string | undefined): string[] =>
      root ? [`${root.replace(/[\\/]+$/, "")}\\Google\\Chrome\\Application\\chrome.exe`] : [];
    return [...join(env["ProgramFiles"]), ...join(env["ProgramFiles(x86)"]), ...join(env["LocalAppData"])];
  }
  if (platform === "darwin") {
    return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"];
  }
  return ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"];
}

const hasFolder = (candidate: string): boolean => /[\\/]/.test(candidate);

/** Start `chrome` on `url` and say whether it started. A program that is not there (ENOENT) is "not found". */
function launch(chrome: string, url: string, spawn: Spawn): Promise<Opened> {
  return new Promise((resolve) => {
    let child: ChildProcess;
    try {
      // Detached with its output dropped, so closing VS Code never closes the browser.
      child = spawn(chrome, [url], { detached: true, stdio: "ignore" });
    } catch (error) {
      resolve({ ok: false, reason: "failed", message: String(error) });
      return;
    }
    child.once("error", (error: NodeJS.ErrnoException) =>
      resolve(
        error.code === "ENOENT"
          ? { ok: false, reason: "not-found", message: `${chrome} was not found.` }
          : { ok: false, reason: "failed", message: error.message },
      ),
    );
    child.once("spawn", () => {
      child.unref();
      resolve({ ok: true, chrome });
    });
  });
}

/**
 * Open `url` in Google Chrome. `configured` is the person's own path to it (the swissarmygrid.chromePath setting) and,
 * when given, is the only place looked: a wrong path is reported, never quietly replaced by another browser.
 */
export async function openInChrome(
  url: string,
  deps: ChromeDeps,
  platform: NodeJS.Platform,
  env: Record<string, string | undefined>,
  configured = "",
): Promise<Opened> {
  const own = configured.trim();
  if (own) {
    if (hasFolder(own) && !deps.exists(own)) {
      return { ok: false, reason: "not-found", message: `The chromePath setting points at ${own}, which does not exist.` };
    }
    return launch(own, url, deps.spawn);
  }
  const candidates = chromeCandidates(platform, env);
  // Real files first (cheap to check), then bare names that only PATH can resolve.
  const ordered = [...candidates.filter((c) => hasFolder(c) && deps.exists(c)), ...candidates.filter((c) => !hasFolder(c))];
  for (const chrome of ordered) {
    const result = await launch(chrome, url, deps.spawn);
    if (result.ok || result.reason !== "not-found") return result;
  }
  return { ok: false, reason: "not-found", message: "Google Chrome was not found on this computer." };
}
