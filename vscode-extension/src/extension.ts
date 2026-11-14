// extension.ts -- SwissArmyGrid in VS Code, and nothing more: its MCP tools as VS Code's own tools (agent mode, any
// model), and a button that opens SwissArmyGrid, as the app's icon does. The server and the tools are installed with uv
// (install.sh, install.ps1); the extension only points VS Code at them.

import { spawn } from "child_process";
import * as fs from "fs";
import * as vscode from "vscode";
import { ChromeDeps, openInChrome } from "./core/chrome";
import { ensureRunning, NOT_INSTALLED, serverPython, workspaceUrl } from "./vscode/server";

export function activate(ctx: vscode.ExtensionContext): void {
  const log = vscode.window.createOutputChannel("SwissArmyGrid");
  const chrome: ChromeDeps = { spawn, exists: fs.existsSync };

  // ===== THE TOOLS: the uv tool install's MCP server, offered to VS Code =====
  const changed = new vscode.EventEmitter<void>();
  ctx.subscriptions.push(
    log,
    changed,
    vscode.lm.registerMcpServerDefinitionProvider("swissarmygrid", {
      onDidChangeMcpServerDefinitions: changed.event,
      provideMcpServerDefinitions: () => {
        const python = serverPython();
        if (!python) {
          log.appendLine(NOT_INSTALLED);
          return [];
        }
        return [new vscode.McpStdioServerDefinition("SwissArmyGrid", python, ["-m", "swissarmygrid.mcp.server"])];
      },
    }),
  );

  // ===== THE BUTTON: start SwissArmyGrid if it is not running, and open it =====
  const open = async (): Promise<void> => {
    try {
      const info = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: "SwissArmyGrid" },
        () => ensureRunning(),
      );
      changed.fire(); // installed now, if it was not when VS Code started
      const url = workspaceUrl(info);
      const own = vscode.workspace.getConfiguration("swissarmygrid").get<string>("chromePath", "");
      const opened = await openInChrome(url, chrome, process.platform, process.env, own);
      if (!opened.ok) {
        log.appendLine(opened.message);
        await vscode.env.openExternal(vscode.Uri.parse(url));
      }
    } catch (error) {
      log.appendLine(String(error));
      void vscode.window.showErrorMessage((error as Error).message);
    }
  };
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  item.text = "$(table) SwissArmyGrid";
  item.command = "swissarmygrid.open";
  item.show();
  ctx.subscriptions.push(item, vscode.commands.registerCommand("swissarmygrid.open", open));
}

export function deactivate(): void {
  // Nothing to stop: SwissArmyGrid keeps running, as the app does, until it is closed.
}
