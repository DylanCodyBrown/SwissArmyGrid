// paths.ts -- where SwissArmyGrid's pieces live on this machine: the connection file the server writes, and the
// Python the extension starts the server with. Pure functions of the environment, so the tests can pass their own.

import * as path from "path";

export type Env = Record<string, string | undefined>;

/** The connection file: SWISSARMYGRID_INFO, else ~/.swissarmygrid/server.json (the server and every client agree). */
export function infoFile(env: Env, home: string): string {
  return env.SWISSARMYGRID_INFO || path.join(home, ".swissarmygrid", "server.json");
}

/**
 * The Pythons that may run the server, in the order to try them:
 *   1. SWISSARMYGRID_SERVER, a python.exe with the server installed (for working on SwissArmyGrid itself: the repo's
 *      .venv). When it is set it is the only one, so a typo fails loudly instead of quietly using another install.
 *   2. The uv tool install that install-sag and this extension make: <uv tool dir>/swissarmygrid/Scripts/python.exe. It is
 *      the one install the extension keeps current, which is why nothing else may start a server. UV_TOOL_DIR, when set,
 *      is uv's tool folder and the only one: uv installs there and nowhere else, so the default folders don't apply.
 */
export function serverPythons(env: Env, home: string, platform: string): string[] {
  if (env.SWISSARMYGRID_SERVER) return [env.SWISSARMYGRID_SERVER];
  // The target platform's path rules, not this machine's: the tests build Windows paths on any host.
  const p = platform === "win32" ? path.win32 : path.posix;
  const python = platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"];
  const roots: string[] = [];
  if (env.UV_TOOL_DIR) roots.push(env.UV_TOOL_DIR);
  else {
    if (platform === "win32" && env.APPDATA) roots.push(p.join(env.APPDATA, "uv", "tools"));
    roots.push(p.join(env.XDG_DATA_HOME || p.join(home, ".local", "share"), "uv", "tools"));
  }
  return roots.map((root) => p.join(root, "swissarmygrid", ...python));
}

/**
 * The Python to *start* the server with: on Windows the pythonw.exe beside python.exe, when there is one.
 *
 * A venv's python.exe is only a launcher: it starts the real interpreter as a child. Node's windowsHide hides the
 * launcher, but the child is a console program with no console to share, so Windows gives it a new, visible one, and
 * closing that window ends the server. pythonw.exe is the same interpreter built without a console, so nothing appears
 * (checked 2026-10-23: one conhost.exe attached with python.exe, none with pythonw.exe, the server answering and
 * writing its log either way). Elsewhere, or without a pythonw.exe, the python itself.
 */
export function windowlessPython(python: string, platform: string, exists: (file: string) => boolean): string {
  if (platform !== "win32" || path.win32.basename(python).toLowerCase() !== "python.exe") return python;
  const pythonw = path.win32.join(path.win32.dirname(python), "pythonw.exe");
  return exists(pythonw) ? pythonw : python;
}

/** The same file named two ways? Windows paths ignore case and either slash. */
export function samePath(a: string, b: string, platform: string): boolean {
  const normalize = platform === "win32" ? path.win32.normalize : path.posix.normalize;
  const norm = (p: string): string => normalize(p).replace(/[\\/]+$/, "");
  return platform === "win32" ? norm(a).toLowerCase() === norm(b).toLowerCase() : norm(a) === norm(b);
}

/**
 * The same install? samePath, except that on Windows pythonw.exe and python.exe in one folder are one interpreter. The
 * extension starts the server with pythonw.exe (see windowlessPython), so the server reports pythonw.exe as the program
 * running it, while the install is found as python.exe: without this every server the extension started itself would
 * look like another copy.
 */
export function sameInstall(a: string, b: string, platform: string): boolean {
  const plain = (p: string): string => (platform === "win32" ? p.replace(/pythonw\.exe$/i, "python.exe") : p);
  return samePath(plain(a), plain(b), platform);
}
