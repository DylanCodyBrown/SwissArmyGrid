// server.ts -- the SwissArmyGrid server, as the Open button needs it: the uv tool install that runs it (and its MCP
// tools), whether it is running, and starting it when it is not. The server is installed with uv (install.sh or
// install.ps1); this extension never installs or updates it.

import { spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { Env, infoFile, serverPythons, windowlessPython } from "../core/paths";

/** The parts of ~/.swissarmygrid/server.json the extension reads. */
export interface ServerInfo {
  port: number;
  token: string;
}

export const NOT_INSTALLED = "SwissArmyGrid is not installed. Install it with uv: sh install.sh <wheel>, or install.ps1 on Windows.";

/** The Python of the uv tool install, or null when SwissArmyGrid is not installed. */
export function serverPython(env: Env = process.env): string | null {
  return serverPythons(env, os.homedir(), process.platform).find((p) => fs.existsSync(p)) ?? null;
}

function readInfo(env: Env): ServerInfo | null {
  try {
    const info = JSON.parse(fs.readFileSync(infoFile(env, os.homedir()), "utf8"));
    return typeof info.port === "number" && typeof info.token === "string" ? info : null;
  } catch {
    return null;
  }
}

/** The running server, or null. A connection file left by a crash names no live server. */
export async function running(env: Env = process.env): Promise<ServerInfo | null> {
  const info = readInfo(env);
  if (!info) return null;
  try {
    const response = await fetch(`http://127.0.0.1:${info.port}/health`, {
      headers: { Authorization: `Bearer ${info.token}` },
      signal: AbortSignal.timeout(2000),
    });
    return response.ok ? info : null;
  } catch {
    return null;
  }
}

/** The running server, started first when it is not running (detached: it outlives VS Code, as the app does). */
export async function ensureRunning(env: Env = process.env): Promise<ServerInfo> {
  const up = await running(env);
  if (up) return up;
  const python = serverPython(env);
  if (!python) throw new Error(NOT_INSTALLED);
  const logPath = path.join(path.dirname(infoFile(env, os.homedir())), "server.log");
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  const out = fs.openSync(logPath, "a");
  try {
    // pythonw.exe on Windows: python.exe would give the server a console window, and closing it ended the server.
    spawn(windowlessPython(python, process.platform, fs.existsSync), ["-m", "swissarmygrid.server"], {
      detached: true,
      windowsHide: true,
      stdio: ["ignore", out, out],
      env: { ...env },
    }).unref();
  } finally {
    fs.closeSync(out);
  }
  for (let waited = 0; waited < 30000; waited += 300) {
    const info = await running(env);
    if (info) return info;
    await new Promise((done) => setTimeout(done, 300));
  }
  throw new Error(`SwissArmyGrid did not start; its output is in ${logPath}.`);
}

/** The page with every open grid as a tab, the token in its #fragment (a browser never sends the fragment). */
export function workspaceUrl(info: ServerInfo): string {
  return `http://127.0.0.1:${info.port}/workspace#token=${encodeURIComponent(info.token)}`;
}
