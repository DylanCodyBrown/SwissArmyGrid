// End to end in a real VS Code: the extension starts the server when VS Code opens, a second server can't start beside
// it, a push from Python shows its grid in VS Code, the push button loads a CSV and the chosen sheets of a workbook, and
// restart gives a fresh server that is still VS Code's. Made-up data only.

import * as assert from "assert";
import { execFileSync, spawnSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import type { SwissArmyGridApi } from "../../../src/extension";

const python = process.env.SAG_TEST_PYTHON!;
const dir = process.env.SAG_TEST_DIR!;

async function until(check: () => boolean, ms: number): Promise<boolean> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (check()) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function rowsOf(api: SwissArmyGridApi, name: string): Promise<number | undefined> {
  const { datasets } = await api.server.call<{ datasets: { name: string; rows: number }[] }>("GET", "/datasets");
  return datasets.find((d) => d.name === name)?.rows;
}

describe("SwissArmyGrid in VS Code, end to end", function () {
  let api: SwissArmyGridApi;

  before(async () => {
    const extension = vscode.extensions.getExtension("dylancodybrown.swissarmygrid")!;
    api = (await extension.activate()) as SwissArmyGridApi;
    await api.ready;
  });

  it("starts the server when VS Code opens, as VS Code's own", () => {
    assert.strictEqual(api.state().kind, "running", JSON.stringify(api.state()));
    assert.strictEqual(api.server.readInfo()!.started_by, "vscode");
  });

  it("a second server can't start beside it", () => {
    const second = spawnSync(python, ["-m", "swissarmygrid.server"], { env: process.env, encoding: "utf8", timeout: 60000 });
    assert.strictEqual(second.status, 1, second.stderr);
    assert.match(second.stderr, /already running/);
  });

  it("a push from Python shows its grid in VS Code", async () => {
    const script = "import swissarmygrid as sag; print(sag.push([{'region': 'North', 'amount': 5}], name='from_python'))";
    assert.match(execFileSync(python, ["-c", script], { env: process.env, encoding: "utf8" }), /\/g\//);
    assert.ok(await until(() => api.opener.lastShown !== null, 15000), "VS Code was never asked to show the grid");
    assert.match(api.opener.lastShown!, /\/workspace#token=/);
    const tabs = vscode.window.tabGroups.all.flatMap((group) => group.tabs.map((tab) => tab.label));
    console.log(`      tabs open: ${JSON.stringify(tabs)}`);
  });

  it("the Open SAG in Chrome command starts Chrome on the running server's workspace address", async () => {
    // Swap the launcher for a recorder (the same object the command closes over), so no browser opens in a test run.
    const real = { spawn: api.chrome.spawn, exists: api.chrome.exists };
    const calls: { command: string; args: string[] }[] = [];
    api.chrome.exists = () => true;
    api.chrome.spawn = ((command: string, args: string[]) => {
      calls.push({ command, args });
      const child: any = new (require("events").EventEmitter)();
      child.unref = () => undefined;
      setImmediate(() => child.emit("spawn"));
      return child;
    }) as any;
    try {
      await vscode.commands.executeCommand("swissarmygrid.openInChrome");
    } finally {
      api.chrome.spawn = real.spawn;
      api.chrome.exists = real.exists;
    }
    assert.strictEqual(calls.length, 1, "Chrome should be started exactly once");
    assert.match(calls[0].args[0], /^http:\/\/127\.0\.0\.1:\d+\/workspace#token=/);
    // VS Code's own tab adds &grid=<id> to focus the pushed table; Chrome gets the workspace itself, same server and token.
    assert.ok(api.opener.lastShown!.startsWith(calls[0].args[0]), `${api.opener.lastShown} vs ${calls[0].args[0]}`);
  });

  it("the push button loads a CSV file", async () => {
    const csv = path.join(dir, "orders.csv");
    fs.writeFileSync(csv, "id,region\n1,North\n2,South\n3,East\n");
    assert.deepStrictEqual(await api.pushFiles([csv]), ["orders"]);
    assert.strictEqual(await rowsOf(api, "orders"), 3);
  });

  it("a workbook with two sheets loads the sheets picked, each as its own table", async () => {
    const book = path.join(dir, "book.xlsx");
    const make = [
      "from openpyxl import Workbook",
      "b = Workbook(); s = b.active; s.title = 'Q1'; s.append(['a']); s.append([1])",
      "t = b.create_sheet('Q2'); t.append(['b']); t.append([2]); t.append([3])",
      `b.save(r'${book}')`,
    ].join("\n");
    execFileSync(python, ["-c", make]);
    assert.deepStrictEqual(await api.pushFiles([book], async (_file, sheets) => sheets), ["book_Q1", "book_Q2"]);
    assert.strictEqual(await rowsOf(api, "book_Q2"), 2);
  });

  it("restart gives a fresh server, still VS Code's, with no tables", async () => {
    const before = api.server.readInfo()!;
    await api.restart(false);
    const after = api.server.readInfo()!;
    assert.notStrictEqual(after.pid, before.pid);
    assert.strictEqual(after.started_by, "vscode");
    assert.strictEqual(api.state().kind, "running");
    assert.strictEqual(await rowsOf(api, "orders"), undefined);
  });

  // ===== keeping the server running =====

  it("starts the server again when it dies without being stopped", async () => {
    const before = api.server.readInfo()!;
    // A hard kill leaves the connection file behind, which is what a crash looks like (a clean stop removes it).
    process.kill(before.pid);
    const restarted = await until(() => {
      const now = api.server.readInfo();
      return now !== null && now.pid !== before.pid;
    }, 60000);
    assert.ok(restarted, "the extension never started the server again");
    assert.ok(await until(() => api.state().kind === "running", 30000), JSON.stringify(api.state()));
    assert.strictEqual(api.server.readInfo()!.started_by, "vscode");
  });

  // ===== keeping it current: the notification, never a silent update =====

  /**
   * Runs `body` as a machine with no server installed: not the developer override, and uv's tool folder an empty
   * folder of its own, so the real install is never touched. Notifications are caught instead of shown, and answered
   * with `answer`. Everything is put back afterwards.
   */
  async function onAMachineWithoutTheServer(
    answer: string | undefined,
    body: (seen: { message: string; items: string[] }[], toolDir: string) => Promise<void>,
  ): Promise<void> {
    const toolDir = fs.mkdtempSync(path.join(os.tmpdir(), "sag-tools-"));
    const names = ["SWISSARMYGRID_SERVER", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR"] as const;
    const saved = Object.fromEntries(names.map((name) => [name, process.env[name]]));
    const seen: { message: string; items: string[] }[] = [];
    const windowApi = vscode.window as unknown as { showInformationMessage: (m: string, ...i: string[]) => Promise<string | undefined> };
    const realMessage = windowApi.showInformationMessage;
    const cache = api.server as unknown as { install: unknown };
    delete process.env.SWISSARMYGRID_SERVER;
    process.env.UV_TOOL_DIR = toolDir;
    process.env.UV_TOOL_BIN_DIR = path.join(toolDir, "bin");
    cache.install = null;
    windowApi.showInformationMessage = async (message, ...items) => {
      // The notice the previous test provokes ("stopped unexpectedly...") can arrive late, into this test's window. It
      // is about that test's server, not about the update question under test, so it is not recorded here.
      if (message.startsWith("SwissArmyGrid stopped unexpectedly")) return undefined;
      seen.push({ message, items });
      return answer;
    };
    try {
      await body(seen, toolDir);
    } finally {
      windowApi.showInformationMessage = realMessage;
      for (const name of names) {
        if (saved[name] === undefined) delete process.env[name];
        else process.env[name] = saved[name];
      }
      cache.install = null;
      // A server from the temp install is stopped by now, so the folder can go. Windows can hold a closing program's files
      // for a few seconds; a temp folder left behind is not a test failure (runTest.ts treats its own the same way).
      // Asynchronous on purpose: the synchronous rmSync retries blocked the extension host for minutes, which is what
      // made this test time out.
      try {
        await fs.promises.rm(toolDir, { recursive: true, force: true, maxRetries: 30, retryDelay: 500 });
      } catch {
        console.warn(`      Could not remove ${toolDir}; delete it by hand.`);
      }
    }
  }

  it("offers to install the server it carries, and does nothing when the answer is Later", async function () {
    const wanted = api.server.bundled();
    if (!wanted) return this.skip(); // a development build with no server inside: run `npm run package` once
    const before = api.server.readInfo()!;
    await onAMachineWithoutTheServer("Later", async (seen, toolDir) => {
      await api.offerUpdate();
      assert.strictEqual(seen.length, 1, JSON.stringify(seen));
      assert.deepStrictEqual(seen[0]!.items, ["Install", "Later"]);
      assert.match(seen[0]!.message, new RegExp(`server \\(${wanted.version.replace(/\./g, "\\.")}\\) installed`));
      assert.deepStrictEqual(fs.readdirSync(toolDir), [], "Later must not install anything");
    });
    assert.strictEqual(api.server.readInfo()!.pid, before.pid, "Later must not touch the running server");
  });

  it("stays quiet under the developer override, even with no install", async function () {
    if (!api.server.bundled()) return this.skip();
    await onAMachineWithoutTheServer("Install", async (seen) => {
      process.env.SWISSARMYGRID_SERVER = python; // the override back on, inside the otherwise-empty machine
      await api.offerUpdate();
      assert.deepStrictEqual(seen, []);
    });
  });

  it("closes the running server, installs the one it carries and starts it when the answer is Install", async function () {
    const wanted = api.server.bundled();
    if (!wanted) return this.skip();
    const before = api.server.readInfo()!;
    await onAMachineWithoutTheServer("Install", async (seen, toolDir) => {
      await api.offerUpdate();
      assert.strictEqual(seen.length, 1, JSON.stringify(seen));
      const after = api.server.readInfo()!;
      assert.notStrictEqual(after.pid, before.pid, "the running server should have been closed and replaced");
      assert.strictEqual(after.version, wanted.version);
      // os.tmpdir() can be Windows' 8.3 short form (DYLAN~1.BRO); the server reports the long one.
      const longToolDir = fs.realpathSync.native(toolDir).toLowerCase();
      assert.ok(fs.realpathSync.native(after.executable!).toLowerCase().startsWith(longToolDir), `ran from ${after.executable}, not the new install`);
      // Stop it before the tool folder goes: Windows will not delete a running program's files.
      await api.server.stop(after);
    });
    await api.restart(false); // back to the developer's own server for whatever runs next
    assert.strictEqual(api.state().kind, "running");
  });
});
