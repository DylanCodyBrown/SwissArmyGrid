// state.ts -- what the status bar says about the server: running (ours), running but another copy, stopped, or not
// installed. A pure comparison of the running server's connection file with the install the extension would start.

import { sameInstall } from "./paths";

/** The parts of ~/.swissarmygrid/server.json the extension reads. */
export interface ServerInfo {
  port: number;
  token: string;
  pid: number;
  version: string;
  /** "vscode" or "cli" (servers from 0.4.5 on). */
  started_by?: string;
  /** The Python running it (servers from 0.4.5 on). */
  executable?: string;
}

/** The install the extension starts: its Python, and the swissarmygrid version installed there. */
export interface Install {
  python: string;
  version: string;
}

export type ServerState =
  | { kind: "starting" }
  | { kind: "running"; info: ServerInfo }
  | { kind: "other"; info: ServerInfo; why: string }
  | { kind: "stopped" }
  | { kind: "missing" };

/**
 * "other" is a server from a different install or version than the one the extension starts (the repo's .venv, uvx's
 * cache, or an install that was updated while the server kept running). It works, but it is the conflict the extension
 * exists to end, so the status bar says so and a click restarts it from the right install.
 */
export function classify(running: ServerInfo | null, install: Install | null, platform: string): ServerState {
  if (running === null) return install === null ? { kind: "missing" } : { kind: "stopped" };
  if (install === null) return { kind: "running", info: running };
  if (running.executable && !sameInstall(running.executable, install.python, platform)) {
    return { kind: "other", info: running, why: `It runs from ${running.executable}, not from the uv tool install.` };
  }
  if (running.version !== install.version) {
    return { kind: "other", info: running, why: `It runs version ${running.version}; ${install.version} is installed.` };
  }
  return { kind: "running", info: running };
}
