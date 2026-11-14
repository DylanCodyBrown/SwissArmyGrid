// Unit tests for the pure parts: which install runs the server and its tools, the Chrome launcher, and that the
// manifest offers the tools and the Open button. Run with mocha in Node.

import * as assert from "assert";
import * as fs from "fs";
import * as path from "path";
import { infoFile, samePath, serverPythons, windowlessPython } from "../src/core/paths";
import { EventEmitter } from "events";
import { chromeCandidates, ChromeDeps, openInChrome } from "../src/core/chrome";
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

  it("has its setting", () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "package.json"), "utf8"));
    assert.strictEqual(manifest.contributes.configuration.properties["swissarmygrid.chromePath"].default, "");
  });
});

describe("the manifest", () => {
  const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../package.json"), "utf8"));

  it("offers SwissArmyGrid's MCP tools to VS Code", () => {
    assert.deepStrictEqual(manifest.contributes.mcpServerDefinitionProviders, [{ id: "swissarmygrid", label: "SwissArmyGrid" }]);
    const source = fs.readFileSync(path.resolve(__dirname, "../../src/extension.ts"), "utf8");
    assert.ok(source.includes('registerMcpServerDefinitionProvider("swissarmygrid"'), "registered under the id the manifest names");
    assert.ok(source.includes('"-m", "swissarmygrid.mcp.server"'), "the uv tool install's own MCP server");
  });

  it("has one command, Open SwissArmyGrid, on every editor and notebook toolbar", () => {
    assert.deepStrictEqual(manifest.contributes.commands.map((c: { command: string }) => c.command), ["swissarmygrid.open"]);
    const isOpen = (item: { command: string; when?: string }) => item.command === "swissarmygrid.open" && item.when === undefined;
    assert.ok(manifest.contributes.menus["editor/title"].some(isOpen));
    assert.ok(manifest.contributes.menus["notebook/toolbar"].some(isOpen));
    const icon = manifest.contributes.commands[0].icon.light;
    assert.ok(fs.existsSync(path.resolve(__dirname, "../..", icon)));
    assert.ok(fs.readFileSync(path.resolve(__dirname, "../../.vscodeignore"), "utf8").includes(`!${icon}`));
  });

  it("needs a VS Code with the MCP server API", () => {
    assert.ok(/\^1\.(10[1-9]|1[1-9]\d)/.test(manifest.engines.vscode), manifest.engines.vscode);
  });
});
