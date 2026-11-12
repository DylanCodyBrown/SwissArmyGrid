// Starts the installed VS Code with a throwaway profile, this extension in development mode and every other extension
// disabled, and runs the suite in ./suite. The server runs from SAG_TEST_PYTHON (default: the repo's
// Python-Imports/swissarmygrid/.venv) with its connection file and state in a temp folder, so the real
// ~/.swissarmygrid is never touched. Once VS Code has closed, it checks the server stopped by itself.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const root = path.resolve(__dirname, "../../..");
  const repo = path.resolve(root, "../..");
  const python =
    process.env.SAG_TEST_PYTHON ?? path.join(repo, "Python-Imports", "swissarmygrid", ".venv", "Scripts", "python.exe");
  if (!fs.existsSync(python)) throw new Error(`No Python with the server at ${python}; set SAG_TEST_PYTHON.`);
  const vscodeExe = process.env.SAG_TEST_VSCODE ?? "C:\\Program Files\\Microsoft VS Code\\Code.exe";
  // Run from a terminal inside VS Code, this is set and would make Code.exe start as plain Node.
  delete process.env.ELECTRON_RUN_AS_NODE;
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "sag-ext-"));
  const profile = path.join(scratch, "profile");
  const workspace = path.join(scratch, "workspace");
  fs.mkdirSync(workspace);
  const infoFile = path.join(scratch, "server.json");
  try {
    await runTests({
      vscodeExecutablePath: vscodeExe,
      extensionDevelopmentPath: root,
      extensionTestsPath: path.resolve(__dirname, "suite/index"),
      extensionTestsEnv: {
        SWISSARMYGRID_INFO: infoFile,
        SWISSARMYGRID_STATE: path.join(scratch, "state"),
        SWISSARMYGRID_SERVER: python,
        SAG_TEST_PYTHON: python,
        SAG_TEST_DIR: workspace,
      },
      launchArgs: [workspace, "--disable-extensions", "--user-data-dir", profile, "--skip-welcome", "--disable-workspace-trust"],
    });
    // VS Code has closed: the server it started must stop by itself (30 seconds with no window, checked every 2).
    const pid = JSON.parse(fs.readFileSync(infoFile, "utf8")).pid as number;
    const started = Date.now();
    while (fs.existsSync(infoFile) && Date.now() - started < 60000) await new Promise((r) => setTimeout(r, 1000));
    if (fs.existsSync(infoFile)) {
      try {
        process.kill(pid);
      } catch {
        // Gone already.
      }
      throw new Error("The server was still running 60 seconds after VS Code closed.");
    }
    console.log(`The server stopped by itself ${Math.round((Date.now() - started) / 1000)} seconds after VS Code closed.`);
  } finally {
    // On Windows a closing VS Code can hold files in the profile for several seconds (EPERM/EBUSY). Retry, then warn:
    // a temp folder left behind isn't a test failure, so it must not change the exit code.
    try {
      fs.rmSync(scratch, { recursive: true, force: true, maxRetries: 30, retryDelay: 500 });
    } catch (err: any) {
      console.warn(`Could not remove ${scratch} (${err.code}); delete it by hand.`);
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
