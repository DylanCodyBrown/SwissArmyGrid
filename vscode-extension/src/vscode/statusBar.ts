// statusBar.ts -- SwissArmyGrid's one piece of VS Code UI: the item at the bottom that says whether the server is up and
// restarts it when clicked. No activity bar icon and no panels: the grids open in VS Code's browser tab.

import * as vscode from "vscode";
import { ServerState } from "../core/state";

export class StatusBar implements vscode.Disposable {
  private readonly item = vscode.window.createStatusBarItem("swissarmygrid.status", vscode.StatusBarAlignment.Right, 100);

  constructor() {
    this.item.name = "SwissArmyGrid";
    this.item.command = "swissarmygrid.restart";
    this.show({ kind: "starting" });
    this.item.show();
  }

  show(state: ServerState): void {
    const [icon, tooltip] = describe(state);
    this.item.text = `${icon} SwissArmyGrid`;
    this.item.tooltip = tooltip;
    const warn = state.kind === "other" || state.kind === "missing";
    this.item.backgroundColor = warn ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  }

  dispose(): void {
    this.item.dispose();
  }
}

function describe(state: ServerState): [string, string] {
  switch (state.kind) {
    case "starting":
      return ["$(sync~spin)", "Starting the SwissArmyGrid server..."];
    case "running":
      return ["$(table)", `SwissArmyGrid ${state.info.version} is running. Click to restart it (open tables close).`];
    case "other":
      return ["$(warning)", `${state.why} Click to restart it from the uv tool install (open tables close).`];
    case "stopped":
      return ["$(circle-slash)", "SwissArmyGrid isn't running. Click to start it."];
    case "missing":
      return ["$(error)", "The SwissArmyGrid server isn't installed. Click for how to install it."];
  }
}
