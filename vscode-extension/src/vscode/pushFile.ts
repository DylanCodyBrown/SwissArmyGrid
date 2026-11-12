// pushFile.ts -- the button at the top of a data file, and on its right-click menu in the Explorer: push the file to a
// grid. The server reads the file itself (POST /datasets/{name}/load, trusted Python on this machine), so a file of any
// size goes straight in and the extension only sends its path. A workbook with several sheets asks which to load.

import * as path from "path";
import * as vscode from "vscode";
import { datasetName, isDataFile } from "../core/names";
import { ServerManager } from "./server";

/** Ask which sheets to load: the chosen names, none when cancelled. The integration tests pass their own. */
export type SheetPicker = (file: string, sheets: string[]) => Promise<string[]>;

export const askForSheets: SheetPicker = async (file, sheets) =>
  (await vscode.window.showQuickPick(sheets, {
    canPickMany: true,
    title: `Which sheets of ${path.basename(file)}?`,
    placeHolder: "Each sheet becomes its own table",
  })) ?? [];

/** Push each data file; returns the datasets made. Errors are shown, not thrown: a menu has no caller to tell. */
export async function pushFiles(server: ServerManager, files: string[], pickSheets: SheetPicker): Promise<string[]> {
  const made: string[] = [];
  for (const file of files.filter(isDataFile)) {
    try {
      const loads = await targets(server, file, pickSheets);
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Pushing ${path.basename(file)} to SwissArmyGrid` },
        async () => {
          for (const [name, sheet] of loads) {
            const body = sheet === undefined ? { path: file } : { path: file, sheet };
            await server.call("POST", `/datasets/${encodeURIComponent(name)}/load`, body);
            made.push(name);
          }
        },
      );
    } catch (error) {
      void vscode.window.showErrorMessage(`SwissArmyGrid: ${(error as Error).message}`);
    }
  }
  return made;
}

/** The (dataset, sheet) pairs to load: one for a plain file or a one-sheet workbook, one per chosen sheet otherwise. */
async function targets(server: ServerManager, file: string, pickSheets: SheetPicker): Promise<[string, string?][]> {
  const base = path.basename(file);
  if (path.extname(file).toLowerCase() !== ".xlsx") return [[datasetName(base)]];
  const { sheets } = await server.call<{ sheets: string[] }>("POST", "/files/sheets", { path: file });
  if (sheets.length === 1) return [[datasetName(base), sheets[0]]];
  return (await pickSheets(file, sheets)).map((sheet): [string, string] => [datasetName(base, sheet), sheet]);
}
