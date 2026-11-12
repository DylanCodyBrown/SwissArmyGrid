// Unit tests for the pure parts: which install starts the server, what the status bar calls a running server, the
// dataset name a file gets, and that the menus offer exactly the files the server reads. Run with mocha in Node.

import * as assert from "assert";
import * as fs from "fs";
import * as path from "path";
import { DATA_FILES, datasetName, isDataFile } from "../src/core/names";
import { infoFile, samePath, serverPythons, windowlessPython } from "../src/core/paths";
import { EventEmitter } from "events";
import { chromeCandidates, ChromeDeps, openInChrome } from "../src/core/chrome";
import { classify, ServerInfo } from "../src/core/state";
import { isOlder, mayRestart, newestWheel, shouldOfferUpdate, wheelVersion } from "../src/core/update";

const RUNNING: ServerInfo = { port: 8790, token: "t", pid: 1, version: "0.4.5" };
const TOOL = "C:\\Users\\me\\AppData\\Roaming\\uv\\tools\\swissarmygrid\\Scripts\\python.exe";

describe("serverPythons", () => {
  it("starts from the uv tool install the installers make", () => {
    const found = serverPythons({ APPDATA: "C:\\Users\\me\\AppData\\Roaming" }, "C:\\Users\\me", "win32");
    assert.strictEqual(found[0], TOOL);
  });
  it("uses only UV_TOOL_DIR when it is set, as uv does", () => {
    const found = serverPythons({ UV_TOOL_DIR: "D:\\uvtools", APPDATA: "C:\\a" }, "C:\\Users\\me", "win32");
    assert.deepStrictEqual(found, [path.win32.join("D:\\uvtools", "swissarmygrid", "Scripts", "python.exe")]);
  });
  it("uses only SWISSARMYGRID_SERVER when it is set (a typo fails, it never falls back)", () => {
    assert.deepStrictEqual(serverPythons({ SWISSARMYGRID_SERVER: "C:\\dev\\python.exe", APPDATA: "C:\\a" }, "C:\\u", "win32"), [
      "C:\\dev\\python.exe",
    ]);
  });
  it("finds the connection file the server and the push client use", () => {
    assert.strictEqual(infoFile({}, "C:\\Users\\me"), path.join("C:\\Users\\me", ".swissarmygrid", "server.json"));
    assert.strictEqual(infoFile({ SWISSARMYGRID_INFO: "C:\\t\\s.json" }, "C:\\Users\\me"), "C:\\t\\s.json");
  });
  it("compares Windows paths without case or trailing slashes", () => {
    assert.ok(samePath("C:\\A\\python.exe", "c:/a/PYTHON.EXE", "win32"));
    assert.ok(!samePath("C:\\A\\python.exe", "C:\\B\\python.exe", "win32"));
  });
});

describe("windowlessPython", () => {
  const tool = path.win32.join("C:", "Users", "x", "AppData", "Roaming", "uv", "tools", "swissarmygrid", "Scripts");
  const has = (...files: string[]) => (file: string) => files.some((f) => samePath(f, file, "win32"));

  it("starts the server with pythonw.exe on Windows, so no console window appears", () => {
    const python = path.win32.join(tool, "python.exe");
    const pythonw = path.win32.join(tool, "pythonw.exe");
    assert.strictEqual(windowlessPython(python, "win32", has(pythonw)), pythonw);
  });
  it("keeps python.exe when there is no pythonw.exe beside it", () => {
    const python = path.win32.join(tool, "python.exe");
    assert.strictEqual(windowlessPython(python, "win32", has()), python);
  });
  it("leaves other platforms and other interpreters alone", () => {
    assert.strictEqual(windowlessPython("/home/x/.local/share/uv/tools/swissarmygrid/bin/python", "linux", () => true), "/home/x/.local/share/uv/tools/swissarmygrid/bin/python");
    const other = path.win32.join("C:", "dev", "py.exe");
    assert.strictEqual(windowlessPython(other, "win32", () => true), other);
  });
});

describe("classify", () => {
  const install = { python: TOOL, version: "0.4.5" };
  it("is running when the server is ours", () => {
    assert.strictEqual(classify({ ...RUNNING, executable: TOOL.toLowerCase() }, install, "win32").kind, "running");
  });
  it("is running when the extension started it with pythonw.exe, the windowless twin of the install's python.exe", () => {
    const windowless = TOOL.replace(/python\.exe$/, "pythonw.exe");
    assert.strictEqual(classify({ ...RUNNING, executable: windowless }, install, "win32").kind, "running");
  });
  it("is another copy when it runs from another install, or another version", () => {
    const venv = classify({ ...RUNNING, executable: "C:\\repo\\.venv\\Scripts\\python.exe" }, install, "win32");
    assert.strictEqual(venv.kind, "other");
    assert.strictEqual(classify({ ...RUNNING, version: "0.4.4" }, install, "win32").kind, "other");
  });
  it("is stopped with an install, missing without one", () => {
    assert.strictEqual(classify(null, install, "win32").kind, "stopped");
    assert.strictEqual(classify(null, null, "win32").kind, "missing");
  });
});

describe("isOlder", () => {
  it("compares dotted versions number by number, not as text", () => {
    assert.strictEqual(isOlder("0.6.1", "0.6.4"), true);
    assert.strictEqual(isOlder("0.9.0", "0.10.0"), true);
    assert.strictEqual(isOlder("0.6.4", "0.6.4"), false);
    assert.strictEqual(isOlder("0.6.5", "0.6.4"), false);
    assert.strictEqual(isOlder("1.0", "1.0.0"), false);
  });
  it("replaces a version it can't read, and never acts on a wanted version it can't read", () => {
    assert.strictEqual(isOlder("", "0.6.4"), true);
    assert.strictEqual(isOlder("0.6.4", "banana"), false);
  });
});

describe("wheelVersion and newestWheel", () => {
  it("reads the version from a wheel's file name", () => {
    assert.strictEqual(wheelVersion("swissarmygrid-0.6.4-py3-none-any.whl"), "0.6.4");
    assert.strictEqual(wheelVersion("other-0.6.4-py3-none-any.whl"), null);
    assert.strictEqual(wheelVersion("swissarmygrid-0.6.4.vsix"), null);
  });
  it("picks the newest wheel by version, ignoring other files", () => {
    const names = ["notes.txt", "swissarmygrid-0.9.0-py3-none-any.whl", "swissarmygrid-0.10.0-py3-none-any.whl"];
    assert.strictEqual(newestWheel(names), "swissarmygrid-0.10.0-py3-none-any.whl");
    assert.strictEqual(newestWheel(["notes.txt"]), null);
  });
});

describe("shouldOfferUpdate", () => {
  const base = { installed: "0.6.1", wanted: "0.6.5", developerOverride: false };
  it("offers when the install is older, or missing", () => {
    assert.strictEqual(shouldOfferUpdate(base), true);
    assert.strictEqual(shouldOfferUpdate({ ...base, installed: null }), true);
  });
  it("stays quiet when the install is current or newer", () => {
    assert.strictEqual(shouldOfferUpdate({ ...base, installed: "0.6.5" }), false);
    assert.strictEqual(shouldOfferUpdate({ ...base, installed: "0.7.0" }), false);
  });
  it("stays quiet with no bundled server (a development build)", () => {
    assert.strictEqual(shouldOfferUpdate({ ...base, wanted: null }), false);
  });
  it("never offers over a developer's own Python (SWISSARMYGRID_SERVER)", () => {
    assert.strictEqual(shouldOfferUpdate({ ...base, developerOverride: true }), false);
    assert.strictEqual(shouldOfferUpdate({ ...base, installed: null, developerOverride: true }), false);
  });
});

describe("mayRestart", () => {
  const now = 10_000_000;
  it("allows a restart until three have happened in ten minutes", () => {
    assert.strictEqual(mayRestart([], now), true);
    assert.strictEqual(mayRestart([now - 1000, now - 2000], now), true);
    assert.strictEqual(mayRestart([now - 1000, now - 2000, now - 3000], now), false);
  });
  it("forgets restarts older than ten minutes", () => {
    const old = now - 11 * 60 * 1000;
    assert.strictEqual(mayRestart([old, old, old], now), true);
  });
});

describe("package.json commands", () => {
  it("offers the update command that the extension registers", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "package.json"), "utf8"));
    assert.ok(pkg.contributes.commands.some((c: { command: string }) => c.command === "swissarmygrid.updateServer"));
  });
});

describe("datasetName", () => {
  it("names a file after its stem, made safe for the server", () => {
    assert.strictEqual(datasetName("orders.csv"), "orders");
    assert.strictEqual(datasetName("Q3 sales (final).csv"), "Q3_sales_final");
    assert.strictEqual(datasetName("book.xlsx", "North America"), "book_North_America");
    assert.strictEqual(datasetName("(((.csv"), "data");
    assert.strictEqual(datasetName(`${"x".repeat(80)}.csv`).length, 64);
  });
  it("knows the data files", () => {
    assert.ok(isDataFile("C:\\data\\Orders.CSV") && isDataFile("book.xlsx"));
    assert.ok(!isDataFile("script.py") && !isDataFile("README"));
  });
});

describe("package.json menus", () => {
  it("offer exactly the files the server reads", () => {
    const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../package.json"), "utf8"));
    const clauses: string[] = [
      ...manifest.contributes.menus["editor/title"],
      ...manifest.contributes.menus["explorer/context"],
    ]
      .filter((item: { command: string }) => item.command === "swissarmygrid.pushFile")
      .map((item: { when: string }) => item.when);
    for (const when of clauses) {
      const [, body, flags] = /=~ \/(.*)\/(\w*)$/.exec(when)!;
      const matches = new RegExp(body, flags);
      for (const ext of DATA_FILES) assert.ok(matches.test(ext.toUpperCase()), `${ext} missing from: ${when}`);
      assert.ok(!matches.test(".py") && !matches.test(".xlsm"));
    }
  });

  it("put the SwissArmyGrid button on every editor and notebook toolbar", () => {
    const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../package.json"), "utf8"));
    const isOpen = (item: { command: string }) => item.command === "swissarmygrid.showGrids";
    const onEditor = manifest.contributes.menus["editor/title"].find(isOpen);
    const onNotebook = manifest.contributes.menus["notebook/toolbar"].find(isOpen);
    assert.ok(onEditor && onEditor.when === undefined, "on every editor, not only Python");
    assert.ok(onNotebook && onNotebook.when === undefined, "on every notebook");
    const command = manifest.contributes.commands.find(isOpen);
    for (const theme of ["light", "dark"]) {
      const file = path.resolve(__dirname, "../..", command.icon[theme]);
      assert.ok(fs.existsSync(file), `${theme} icon missing: ${file}`);
    }
    const shipped = fs.readFileSync(path.resolve(__dirname, "../../.vscodeignore"), "utf8");
    assert.ok(shipped.includes(`!${command.icon.light}`), "the icon must be allowed into the .vsix");
  });
});

describe("Open SAG in Chrome", () => {
  const URL = "http://127.0.0.1:8790/workspace#token=abc";
  /** Windows paths are built, not typed, so the backslashes cannot go wrong. */
  const win = (...parts: string[]): string => path.win32.join(...parts);
  const PROGRAM_FILES = "C:\\Program Files";
  const chromeIn = (root: string): string => win(root, "Google", "Chrome", "Application", "chrome.exe");

  /** A stand-in for child_process.spawn: records each call, and starts or fails the way the test says. */
  function launcher(outcome: (command: string) => "start" | NodeJS.ErrnoException, present: string[] = []) {
    const calls: { command: string; args: string[]; options: any; unrefd: boolean }[] = [];
    const deps: ChromeDeps = {
      exists: (p) => present.includes(p),
      spawn: ((command: string, args: string[], options: any) => {
        const child: any = new EventEmitter();
        const call = { command, args, options, unrefd: false };
        child.unref = () => (call.unrefd = true);
        calls.push(call);
        const result = outcome(command);
        setImmediate(() => (result === "start" ? child.emit("spawn") : child.emit("error", result)));
        return child;
      }) as any,
    };
    return { deps, calls };
  }
  const missing = (): NodeJS.ErrnoException => Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });

  it("looks where Chrome installs on each system, and skips folders the environment does not name", () => {
    const env = {
      ProgramFiles: PROGRAM_FILES,
      "ProgramFiles(x86)": "C:\\Program Files (x86)\\",
      LocalAppData: "C:\\Users\\a\\AppData\\Local",
    };
    assert.deepStrictEqual(chromeCandidates("win32", env), [
      chromeIn(PROGRAM_FILES),
      chromeIn("C:\\Program Files (x86)"),
      chromeIn("C:\\Users\\a\\AppData\\Local"),
    ]);
    assert.deepStrictEqual(chromeCandidates("win32", {}), []);
    assert.deepStrictEqual(chromeCandidates("darwin", {}), ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]);
    assert.ok(chromeCandidates("linux", {}).includes("google-chrome"));
  });

  it("starts the first Chrome that is there, on the address, detached from VS Code", async () => {
    const found = chromeIn(PROGRAM_FILES);
    const { deps, calls } = launcher(() => "start", [found]);
    const result = await openInChrome(URL, deps, "win32", { ProgramFiles: PROGRAM_FILES });
    assert.deepStrictEqual(result, { ok: true, chrome: found });
    assert.strictEqual(calls.length, 1);
    assert.deepStrictEqual(calls[0].args, [URL]);
    assert.strictEqual(calls[0].options.detached, true);
    assert.strictEqual(calls[0].options.stdio, "ignore");
    assert.strictEqual(calls[0].unrefd, true, "closing VS Code must not close the browser");
  });

  it("uses the person's own path when they set one, and only that", async () => {
    const mine = win("D:\\", "tools", "chrome.exe");
    const { deps, calls } = launcher(() => "start", [mine]);
    assert.deepStrictEqual(await openInChrome(URL, deps, "win32", {}, `  ${mine}  `), { ok: true, chrome: mine });
    assert.strictEqual(calls[0].command, mine);
  });

  it("says so when the person's path does not exist, instead of trying another browser", async () => {
    const { deps, calls } = launcher(() => "start", [chromeIn(PROGRAM_FILES)]);
    const wrong = win("D:\\", "nope", "chrome.exe");
    const result = await openInChrome(URL, deps, "win32", { ProgramFiles: PROGRAM_FILES }, wrong);
    assert.strictEqual(result.ok, false);
    if (!result.ok) {
      assert.strictEqual(result.reason, "not-found");
      assert.ok(result.message.includes(`chromePath setting points at ${wrong}, which does not exist`), result.message);
    }
    assert.strictEqual(calls.length, 0);
  });

  it("tries the next name when one is not installed, then reports that Chrome is not found", async () => {
    const tried = launcher((command) => (command === "chromium" ? "start" : missing()));
    const ok = await openInChrome(URL, tried.deps, "linux", {});
    assert.deepStrictEqual(ok, { ok: true, chrome: "chromium" });
    assert.deepStrictEqual(tried.calls.map((c) => c.command), ["google-chrome", "google-chrome-stable", "chromium"]);

    const none = launcher(() => missing());
    const result = await openInChrome(URL, none.deps, "linux", {});
    assert.strictEqual(result.ok, false);
    if (!result.ok) assert.match(result.message, /Google Chrome was not found/);
  });

  it("stops at an error that is not 'not installed', and says what it was", async () => {
    const denied = Object.assign(new Error("spawn EACCES"), { code: "EACCES" });
    const { deps, calls } = launcher(() => denied);
    const result = await openInChrome(URL, deps, "linux", {});
    assert.strictEqual(result.ok, false);
    if (!result.ok) {
      assert.strictEqual(result.reason, "failed");
      assert.match(result.message, /EACCES/);
    }
    assert.strictEqual(calls.length, 1);
  });

  it("reports a launcher that throws", async () => {
    const deps: ChromeDeps = {
      exists: () => true,
      spawn: (() => {
        throw new Error("boom");
      }) as any,
    };
    const result = await openInChrome(URL, deps, "darwin", {});
    assert.strictEqual(result.ok, false);
    if (!result.ok) assert.match(result.message, /boom/);
  });

  it("is a command with a toolbar button and a setting", () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "package.json"), "utf8"));
    const ids = manifest.contributes.commands.map((c: any) => c.command);
    assert.ok(ids.includes("swissarmygrid.openInChrome"));
    assert.ok(manifest.contributes.menus["editor/title"].some((m: any) => m.command === "swissarmygrid.openInChrome"));
    assert.ok(manifest.contributes.menus["notebook/toolbar"].some((m: any) => m.command === "swissarmygrid.openInChrome"));
    assert.strictEqual(manifest.contributes.configuration.properties["swissarmygrid.chromePath"].default, "");
  });
});
