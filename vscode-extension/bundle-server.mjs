// bundle-server.mjs -- put the SwissArmyGrid server inside the extension, so the extension can install and update it.
// `npm run package` runs it just before the .vsix is made. It copies the newest wheel from the server project's dist/
// folder (build it first with `uv build --wheel`; build.py --vsix does) into ./server/, which .vscodeignore lets into
// the package and .gitignore keeps out of git. Set SWISSARMYGRID_WHEEL_DIR to take the wheel from another folder.
//
// The extension and the server it carries share a version number, so "extension 0.6.5" always means "server 0.6.5". This
// refuses to build when they differ: a mismatch would make the installed extension and the installed server disagree
// about what is current.

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const wheelDir = process.env.SWISSARMYGRID_WHEEL_DIR ?? path.resolve(here, "../dist");
const { version } = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8"));

const versionOf = (file) => /^swissarmygrid-(\d+(?:\.\d+)*)-.*\.whl$/.exec(file)?.[1] ?? null;
const compare = (a, b) => {
  const x = a.split(".").map(Number);
  const y = b.split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) - (y[i] ?? 0);
  return 0;
};

if (!fs.existsSync(wheelDir)) throw new Error(`No server wheel folder at ${wheelDir}. Build the wheel first: uv build --wheel (in the repository root, or run python build.py).`);
const wheels = fs.readdirSync(wheelDir).filter((f) => versionOf(f)).sort((a, b) => compare(versionOf(a), versionOf(b)));
if (wheels.length === 0) throw new Error(`No swissarmygrid-*.whl in ${wheelDir}. Build it first: uv build --wheel (in the repository root, or run python build.py).`);
const wheel = wheels[wheels.length - 1];
if (versionOf(wheel) !== version) {
  throw new Error(
    `The newest server wheel is ${versionOf(wheel)} but this extension is ${version}. They share a version: ` +
      `bump package.json (and package-lock.json) to ${versionOf(wheel)}, or build the matching wheel.`,
  );
}

const out = path.join(here, "server");
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out);
fs.copyFileSync(path.join(wheelDir, wheel), path.join(out, wheel));
console.log(`Bundled the server: ${wheel}`);
