// server.ts -- the one SwissArmyGrid server: find it, start it, stop it, call it. VS Code owns the server, and this is
// the only code anywhere that starts one (the push client, the MCP and the prompt hook only connect), always from the
// same install, so every script, notebook and agent reaches one server of one version. Think of it as a hosted service
// the IDE starts and supervises; the server's own instance lock stops two windows starting two at once.

import { execFile, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { Env, infoFile, serverPythons, windowlessPython } from "../core/paths";
import { classify, Install, ServerInfo, ServerState } from "../core/state";
import { newestWheel, wheelVersion } from "../core/update";

// The case where this build carries no server to install (a development build): the extension can't fix it itself.
export const NOT_INSTALLED =
  "The SwissArmyGrid server isn't installed on this machine. Run the installer from a SwissArmyGrid release " +
  "(installer/install.ps1 on Windows, installer/install.sh elsewhere), or install the wheel as a uv tool: " +
  'uv tool install "swissarmygrid-<version>-py3-none-any.whl[server]".';

/** The command offered to copy when the server is missing: the uv tool install of a release's wheel. */
export const INSTALL_SERVER = 'uv tool install "swissarmygrid-<version>-py3-none-any.whl[server]"';

/** The command that installs uv, offered when an update needs it and it isn't there. */
export const INSTALL_UV = "winget install --id=astral-sh.uv -e";

/** uv, the tool that installs the server, is not on this machine. */
export class UvMissingError extends Error {
  constructor() {
    super(`The SwissArmyGrid server is installed with uv, which isn't on this machine. Install it with: ${INSTALL_UV}`);
  }
}

/** The server this extension carries: a wheel inside the extension, and the version in its file name. */
export interface BundledServer {
  path: string;
  version: string;
}

export class ServerManager {
  private install: Promise<Install | null> | null = null;

  /** `bundleDir` is the extension's own server folder, where the build puts the wheel; absent in a development build. */
  constructor(
    private readonly log: vscode.OutputChannel,
    private readonly env: Env = process.env,
    private readonly bundleDir: string | null = null,
  ) {}

  /** SWISSARMYGRID_SERVER names a developer's own Python: this extension must never install over it. */
  get developerOverride(): boolean {
    return Boolean(this.env.SWISSARMYGRID_SERVER);
  }

  /** The server wheel this extension carries, or null (a development build has none). */
  bundled(): BundledServer | null {
    if (!this.bundleDir) return null;
    try {
      const name = newestWheel(fs.readdirSync(this.bundleDir));
      return name ? { path: path.join(this.bundleDir, name), version: wheelVersion(name)! } : null;
    } catch {
      return null;
    }
  }

  /**
   * Install the bundled server as the uv tool: the same command the installers (installer/install.ps1, install.sh) run, so the extension and the
   * script leave the machine the same way. The running server must be stopped first (Windows locks a running
   * program's files). Throws UvMissingError when there is no uv, or uv's own message when the install fails.
   */
  async installBundled(wheel: BundledServer): Promise<void> {
    const uv = await this.findUv();
    if (!uv) throw new UvMissingError();
    await this.uvInstall(uv, wheel, "server");
    this.install = null; // the install on disk changed: look again
  }

  private uvInstall(uv: string, wheel: BundledServer, extras: string): Promise<void> {
    const args = ["tool", "install", "--quiet", "--force", "--python", ">=3.12", `${wheel.path}[${extras}]`];
    this.log.appendLine(`Installing the SwissArmyGrid server ${wheel.version}: ${uv} ${args.join(" ")}`);
    return new Promise<void>((resolve, reject) =>
      execFile(uv, args, { timeout: 600000, windowsHide: true, env: this.env, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
        if (stdout.trim()) this.log.appendLine(stdout.trim());
        if (stderr.trim()) this.log.appendLine(stderr.trim());
        if (!error) return resolve();
        const reason = stderr.trim().split(/\r?\n/).slice(-3).join(" ") || error.message;
        reject(new Error(`uv could not install the server: ${reason}`));
      }),
    );
  }

  /** uv from PATH, else where winget and the uv installer put it (VS Code's PATH may predate a fresh install). */
  private async findUv(): Promise<string | null> {
    const exe = process.platform === "win32" ? "uv.exe" : "uv";
    const candidates = [
      "uv",
      path.join(os.homedir(), ".local", "bin", exe),
      ...(this.env.LOCALAPPDATA ? [path.join(this.env.LOCALAPPDATA, "Microsoft", "WinGet", "Links", exe)] : []),
      path.join(os.homedir(), ".cargo", "bin", exe),
    ];
    for (const candidate of candidates) {
      const works = await new Promise<boolean>((resolve) =>
        execFile(candidate, ["--version"], { timeout: 15000, windowsHide: true, env: this.env }, (error) => resolve(!error)),
      );
      if (works) return candidate;
    }
    return null;
  }

  get infoPath(): string {
    return infoFile(this.env, os.homedir());
  }

  /** The connection file's contents, or null (no server, or a file half written). */
  readInfo(): ServerInfo | null {
    try {
      const info = JSON.parse(fs.readFileSync(this.infoPath, "utf8")) as ServerInfo;
      return typeof info.port === "number" && typeof info.token === "string" ? info : null;
    } catch {
      return null;
    }
  }

  /** The running server, or null. A connection file left by a crash names no live server. */
  async running(): Promise<ServerInfo | null> {
    const info = this.readInfo();
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

  /** The install the server starts from, with its version: looked up once per window. */
  installed(): Promise<Install | null> {
    this.install ??= this.findInstall();
    return this.install;
  }

  async state(): Promise<ServerState> {
    return classify(await this.running(), await this.installed(), process.platform);
  }

  /**
   * Start the server from the install and wait until it answers. Detached and hidden, so a window reload doesn't end
   * it; the server ends itself once no VS Code window has been connected for a while. If another window started one at
   * the same moment, ours stops at the instance lock and this adopts theirs.
   */
  async start(): Promise<ServerInfo> {
    const install = await this.installed();
    if (!install) throw new Error(NOT_INSTALLED);
    const logPath = path.join(path.dirname(this.infoPath), "server.log");
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    const out = fs.openSync(logPath, "a");
    // pythonw.exe on Windows: python.exe would give the server a console window of its own, and closing it ended
    // the server (see windowlessPython).
    const python = windowlessPython(install.python, process.platform, fs.existsSync);
    try {
      spawn(python, ["-m", "swissarmygrid.server"], {
        detached: true,
        windowsHide: true,
        stdio: ["ignore", out, out],
        env: { ...this.env, SWISSARMYGRID_STARTED_BY: "vscode" },
      }).unref();
    } finally {
      fs.closeSync(out);
    }
    this.log.appendLine(`Started ${python} -m swissarmygrid.server (its output: ${logPath}).`);
    if (!(await until(async () => (await this.running()) !== null, 30000))) {
      throw new Error(`The server didn't start; its output is in ${logPath}.`);
    }
    return (await this.running())!;
  }

  /** Ask the server to stop and wait until it has; one that won't is ended. */
  async stop(info: ServerInfo): Promise<void> {
    await fetch(`http://127.0.0.1:${info.port}/shutdown`, {
      method: "POST",
      headers: { Authorization: `Bearer ${info.token}` },
      signal: AbortSignal.timeout(5000),
    }).catch(() => undefined);
    if (!(await until(async () => (await this.running()) === null, 10000))) {
      this.log.appendLine(`The server (process ${info.pid}) didn't stop when asked; ending it.`);
      try {
        process.kill(info.pid);
      } catch {
        // Gone already.
      }
    }
  }

  /** One call to the server's HTTP API. A refusal throws with the server's own plain-language reason. */
  async call<T>(method: string, route: string, body?: unknown): Promise<T> {
    const info = await this.running();
    if (!info) throw new Error("SwissArmyGrid isn't running. Click SwissArmyGrid in the status bar to start it.");
    const response = await fetch(`http://127.0.0.1:${info.port}${route}`, {
      method,
      headers: { Authorization: `Bearer ${info.token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(300000),
    });
    if (!response.ok) throw new Error((await response.text()) || response.statusText);
    return (await response.json()) as T;
  }

  private async findInstall(): Promise<Install | null> {
    const python = serverPythons(this.env, os.homedir(), process.platform).find((p) => fs.existsSync(p));
    if (!python) return null;
    const version = await new Promise<string>((resolve) =>
      execFile(
        python,
        ["-c", "import swissarmygrid; print(swissarmygrid.__version__)"],
        { timeout: 30000, windowsHide: true },
        (error, stdout) => resolve(error ? "" : stdout.trim()),
      ),
    );
    if (!version) this.log.appendLine(`${python} has no swissarmygrid server installed.`);
    return version ? { python, version } : null;
  }
}

/** The workspace page: every grid as a tab. The token rides in the fragment, which the page never sends back. */
export function workspaceUrl(info: ServerInfo): string {
  return `http://127.0.0.1:${info.port}/workspace#token=${info.token}`;
}

async function until(check: () => Promise<boolean>, ms: number): Promise<boolean> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}
