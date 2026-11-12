// extension.ts -- SwissArmyGrid lives in VS Code. When VS Code opens, this starts the one SwissArmyGrid server (or adopts
// the one already running), keeps the status bar item current, shows the grids in VS Code's browser tab when the server
// asks, and pushes data files from the button at the top of the file. It also keeps the server current and running: the
// extension carries the server it expects, and when the install is missing or older a notification offers to close the
// server and update it (never silently: the update closes every open table); a server that dies is started again.
// The server stops by itself once no VS Code window has been connected for a while, so closing VS Code ends it. This
// file only wires the parts together.
//
// Plan: Plans/vscode-extension.plan.md

import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { ChromeDeps, openInChrome } from "./core/chrome";
import { ServerState } from "./core/state";
import { mayRestart, shouldOfferUpdate } from "./core/update";
import { Opener } from "./vscode/opener";
import { askForSheets, pushFiles, SheetPicker } from "./vscode/pushFile";
import { INSTALL_SERVER, INSTALL_UV, NOT_INSTALLED, ServerManager, UvMissingError, workspaceUrl } from "./vscode/server";
import { StatusBar } from "./vscode/statusBar";

const CHECK_EVERY_MS = 5000;

/** What activate returns: used only by the integration tests, to drive the extension without clicking. */
export interface SwissArmyGridApi {
  server: ServerManager;
  opener: Opener;
  /** The start-or-adopt that runs when VS Code opens. */
  ready: Promise<void>;
  state: () => ServerState;
  restart: (confirm?: boolean) => Promise<void>;
  /** The notification that offers to close the server and update it; `manual` is the command's "up to date" answer. */
  offerUpdate: (manual?: boolean) => Promise<void>;
  pushFiles: (files: string[], pickSheets?: SheetPicker) => Promise<string[]>;
  /** What the Open SAG in Chrome button starts Chrome with; the integration tests swap in a recorder. */
  chrome: ChromeDeps;
}

export function activate(ctx: vscode.ExtensionContext): SwissArmyGridApi {
  const log = vscode.window.createOutputChannel("SwissArmyGrid");
  // The build puts the server wheel in <extension>/server (bundle-server.mjs); a development build has none.
  const server = new ServerManager(log, process.env, path.join(ctx.extensionPath, "server"));
  const status = new StatusBar();
  const chrome: ChromeDeps = { spawn, exists: fs.existsSync };
  const opener = new Opener(log);
  ctx.subscriptions.push(log, status, opener);
  let state: ServerState = { kind: "starting" };
  let busy = false; // a start or restart is under way: the periodic check leaves the item alone
  const restarts: number[] = []; // when the server was last started again after dying (see check)

  const show = (next: ServerState): void => {
    state = next;
    status.show(next);
    opener.follow(next.kind === "running" || next.kind === "other" ? next.info : null);
  };
  const check = async (): Promise<void> => {
    if (busy) return;
    const next = await server.state();
    // KEEP IT RUNNING: a server that was up and is now gone, but left its connection file, died instead of being stopped
    // (a clean stop, such as the tray icon's, removes the file, and then it stays stopped until asked). Start it again,
    // up to three times in ten minutes so one that crashes on start doesn't loop.
    const wasUp = state.kind === "running" || state.kind === "other";
    if (next.kind === "stopped" && wasUp && server.readInfo() !== null) {
      if (mayRestart(restarts, Date.now())) {
        restarts.push(Date.now());
        log.appendLine("The server stopped without being asked to; starting it again.");
        await startAgain();
        return;
      }
      log.appendLine("The server keeps stopping; leaving it stopped. Click SwissArmyGrid in the status bar to start it.");
    }
    show(next);
  };
  const startAgain = async (): Promise<void> => {
    busy = true;
    status.show({ kind: "starting" });
    try {
      await server.start();
      void vscode.window.showInformationMessage("SwissArmyGrid stopped unexpectedly and was started again. Its open tables closed; push them again.");
    } catch (error) {
      failed(error);
    } finally {
      busy = false;
      show(await server.state());
    }
  };
  const failed = (error: unknown): void => {
    log.appendLine(String(error));
    void vscode.window.showErrorMessage(`SwissArmyGrid: ${(error as Error).message}`);
  };

  // ===== START OR ADOPT: VS Code opening starts the server, unless one is up already =====
  const startOrAdopt = async (): Promise<void> => {
    busy = true;
    status.show({ kind: "starting" });
    try {
      if ((await server.state()).kind === "stopped") await server.start();
    } catch (error) {
      failed(error);
    } finally {
      busy = false;
      await check();
    }
    void offerUpdate(false); // after the server is up, so it works while the notification waits; not awaited
  };

  // ===== KEEP IT CURRENT: offer to close the server and update it =====
  const offerUpdate = async (manual: boolean): Promise<void> => {
    try {
      const wanted = server.bundled();
      const install = await server.installed();
      const installed = install?.version ?? null;
      if (!shouldOfferUpdate({ installed, wanted: wanted?.version ?? null, developerOverride: server.developerOverride })) {
        if (manual) {
          const why =
            wanted === null
              ? "This build of the extension carries no server to install."
              : server.developerOverride
                ? "SWISSARMYGRID_SERVER names your own Python, which is never replaced."
                : `The server is up to date (${installed}).`;
          void vscode.window.showInformationMessage(`SwissArmyGrid: ${why}`);
        }
        return;
      }
      const running = await server.running();
      const { datasets } = running
        ? await server.call<{ datasets: unknown[] }>("GET", "/datasets").catch(() => ({ datasets: [] }))
        : { datasets: [] };
      const closes = datasets.length === 0 ? "" : ` and its ${datasets.length === 1 ? "1 open table" : `${datasets.length} open tables`} (push them again after)`;
      const action = installed ? "Close and update" : "Install";
      const message = installed
        ? `A newer SwissArmyGrid server is available: ${wanted!.version} (you have ${installed}). Updating closes the running server${closes}, then starts the new one.`
        : `SwissArmyGrid needs its server (${wanted!.version}) installed on this machine.`;
      if ((await vscode.window.showInformationMessage(message, action, "Later")) !== action) return;
      await applyUpdate(wanted!);
    } catch (error) {
      failed(error);
    }
  };
  const applyUpdate = async (wanted: { path: string; version: string }): Promise<void> => {
    busy = true;
    status.show({ kind: "starting" });
    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Updating the SwissArmyGrid server to ${wanted.version}...` },
        async () => {
          const running = await server.running();
          if (running) await server.stop(running);
          await server.installBundled(wanted);
        },
      );
    } catch (error) {
      if (error instanceof UvMissingError) {
        const copy = await vscode.window.showErrorMessage(error.message, "Copy the command");
        if (copy) await vscode.env.clipboard.writeText(INSTALL_UV);
      } else failed(error);
    }
    try {
      // Start whatever is installed now: the new server, or the old one again when the update could not be done.
      if ((await server.state()).kind === "stopped") await server.start();
    } catch (error) {
      failed(error);
    } finally {
      busy = false;
      await check();
    }
  };

  // ===== STATUS BAR ITEM: click restarts (or starts, or says how to install) =====
  const restart = async (confirm = true): Promise<void> => {
    if (state.kind === "missing") {
      const copy = await vscode.window.showErrorMessage(NOT_INSTALLED, "Copy the command");
      if (copy) await vscode.env.clipboard.writeText(INSTALL_SERVER);
      return;
    }
    const running = await server.running();
    if (running && confirm) {
      const { datasets } = await server.call<{ datasets: unknown[] }>("GET", "/datasets").catch(() => ({ datasets: [] }));
      if (datasets.length > 0) {
        const tables = datasets.length === 1 ? "1 open table closes" : `${datasets.length} open tables close`;
        const answer = await vscode.window.showWarningMessage(
          `Restart SwissArmyGrid? Its ${tables}; push them again after.`,
          { modal: true },
          "Restart",
        );
        if (answer !== "Restart") return;
      }
    }
    busy = true;
    status.show({ kind: "starting" });
    try {
      if (running) await server.stop(running);
      await server.start();
    } catch (error) {
      failed(error);
    } finally {
      busy = false;
      await check();
    }
  };

  ctx.subscriptions.push(
    vscode.commands.registerCommand("swissarmygrid.restart", () => restart()),
    vscode.commands.registerCommand("swissarmygrid.updateServer", () => offerUpdate(true)),
    // ===== PUSH BUTTON: the top of a data file, and its right-click menu in the Explorer =====
    vscode.commands.registerCommand("swissarmygrid.pushFile", async (uri?: vscode.Uri, uris?: vscode.Uri[]) => {
      const picked = uris && uris.length > 0 ? uris : [uri ?? vscode.window.activeTextEditor?.document.uri];
      const files = picked.filter((u): u is vscode.Uri => u !== undefined && u.scheme === "file").map((u) => u.fsPath);
      if (files.length === 0) {
        void vscode.window.showInformationMessage("Open or select a data file to push it to SwissArmyGrid.");
        return;
      }
      if (!(await server.running())) await startOrAdopt(); // VS Code owns the server: bring it up first
      await pushFiles(server, files, askForSheets);
    }),
    vscode.commands.registerCommand("swissarmygrid.showGrids", async () => {
      const info = await server.running();
      if (info) await opener.show(workspaceUrl(info));
      else void vscode.window.showInformationMessage("SwissArmyGrid isn't running. Click it in the status bar to start it.");
    }),
    // ===== OPEN IN CHROME: the same workspace page as the VS Code tab, full size in the browser you already use =====
    vscode.commands.registerCommand("swissarmygrid.openInChrome", async () => {
      const info = await server.running();
      if (!info) {
        void vscode.window.showInformationMessage("SwissArmyGrid isn't running. Click it in the status bar to start it.");
        return;
      }
      const url = workspaceUrl(info);
      const own = vscode.workspace.getConfiguration("swissarmygrid").get<string>("chromePath", "");
      const result = await openInChrome(url, chrome, process.platform, process.env, own);
      if (result.ok) {
        log.appendLine(`Opened ${url.split("#")[0]} in ${result.chrome}`);
        return;
      }
      log.appendLine(`Open SAG in Chrome: ${result.message}`);
      const fallback = "Open in the default browser";
      const choice = await vscode.window.showWarningMessage(
        `${result.message} Set the swissarmygrid.chromePath setting to its full path, or use the default browser.`,
        fallback,
      );
      if (choice === fallback) await vscode.env.openExternal(vscode.Uri.parse(url));
    }),
  );

  const timer = setInterval(() => void check(), CHECK_EVERY_MS);
  ctx.subscriptions.push({ dispose: () => clearInterval(timer) });
  return {
    server,
    opener,
    ready: startOrAdopt(),
    state: () => state,
    restart,
    offerUpdate: (manual = false) => offerUpdate(manual),
    pushFiles: (files, pickSheets = askForSheets) => pushFiles(server, files, pickSheets),
    chrome,
  };
}

export function deactivate(): void {
  // Nothing to stop: the server notices the window is gone and stops itself once every window is.
}
