// opener.ts -- this window's line to the server (/ws?opener=1). When a push or an agent finds no page showing its grid,
// the server asks the VS Code window the user was last in to show the workspace, and that request lands here: the
// grids open in VS Code's own browser tab, never a separate browser. The line also tells the server when this window
// comes to the front, and while it is open the server knows VS Code is (a server VS Code started stops once no window
// has been connected for a while).

import * as vscode from "vscode";
import { ServerInfo } from "../core/state";

export class Opener implements vscode.Disposable {
  private socket: WebSocket | null = null;
  private server = ""; // "port:token" of the server the socket is for
  private readonly focus: vscode.Disposable;
  /** The last workspace address shown here (the integration tests read it). */
  lastShown: string | null = null;

  constructor(private readonly log: vscode.OutputChannel) {
    this.focus = vscode.window.onDidChangeWindowState((state) => {
      if (state.focused) this.sayFocused();
    });
  }

  /** Stay connected to `info`'s server (a no-op while already connected); null hangs up. */
  follow(info: ServerInfo | null): void {
    const key = info ? `${info.port}:${info.token}` : "";
    if (key === this.server && (this.socket || !info)) return;
    this.socket?.close();
    this.socket = null;
    this.server = key;
    if (!info) return;
    if (typeof WebSocket !== "function") {
      this.log.appendLine("This VS Code is too old to show grids by itself (no WebSocket in its Node); update VS Code.");
      return;
    }
    const socket = new WebSocket(`ws://127.0.0.1:${info.port}/ws?opener=1`, ["swissarmygrid", info.token]);
    socket.addEventListener("open", () => {
      if (vscode.window.state.focused) this.sayFocused();
    });
    socket.addEventListener("message", (event) => this.onNotice(String(event.data)));
    socket.addEventListener("close", () => {
      if (this.socket === socket) this.socket = null; // the next status check reconnects
    });
    socket.addEventListener("error", () => undefined); // a close follows
    this.socket = socket;
  }

  /** Show the workspace in VS Code's browser tab. */
  async show(url: string): Promise<void> {
    this.lastShown = url;
    try {
      await vscode.commands.executeCommand("simpleBrowser.show", url);
    } catch (error) {
      this.log.appendLine(`VS Code couldn't show the grids: ${String(error)}`);
    }
  }

  dispose(): void {
    this.focus.dispose();
    this.socket?.close();
    this.socket = null;
  }

  private onNotice(data: string): void {
    try {
      const notice = JSON.parse(data) as { type?: string; url?: string };
      if (notice.type === "open_workspace" && notice.url) void this.show(notice.url);
    } catch {
      // Every other notice (dataset_updated, grid_shown, ...) is for the pages.
    }
  }

  private sayFocused(): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send('{"type":"focus"}');
  }
}
