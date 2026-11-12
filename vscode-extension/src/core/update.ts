// update.ts -- keeping the server current and running. Pure decisions with no VS Code or disk in them, so the unit tests
// can cover every case: is the installed server older than the one this extension carries, what to do about it given
// the tables open right now, and whether a server that died may be started again.

/** "0.6.4" as [0, 6, 4], or null when it isn't a plain dotted version. */
export function parseVersion(text: string): number[] | null {
  return /^\d+(\.\d+)*$/.test(text.trim()) ? text.trim().split(".").map(Number) : null;
}

/**
 * Is the installed server older than the wanted one? A version we can't read counts as older, so a damaged install is
 * replaced; a wanted version we can't read never triggers anything.
 */
export function isOlder(installed: string, wanted: string): boolean {
  const want = parseVersion(wanted);
  if (!want) return false;
  const have = parseVersion(installed);
  if (!have) return true;
  for (let i = 0; i < Math.max(have.length, want.length); i++) {
    const a = have[i] ?? 0;
    const b = want[i] ?? 0;
    if (a !== b) return a < b;
  }
  return false;
}

/** The version in a wheel's file name: "swissarmygrid-0.6.4-py3-none-any.whl" is "0.6.4". */
export function wheelVersion(fileName: string): string | null {
  const match = /^swissarmygrid-(\d+(?:\.\d+)*)-.*\.whl$/.exec(fileName);
  return match ? match[1]! : null;
}

/** The newest swissarmygrid wheel among these file names, or null. */
export function newestWheel(fileNames: string[]): string | null {
  let best: string | null = null;
  for (const name of fileNames) {
    const version = wheelVersion(name);
    if (!version) continue;
    if (best === null || isOlder(wheelVersion(best)!, version)) best = name;
  }
  return best;
}

/**
 * Should VS Code offer to install or update the server? Never without asking: the update closes the running server and
 * every table open on it, so the person always chooses. No when the install is current, when there is nothing to compare
 * with (a development build carries no server), or when SWISSARMYGRID_SERVER names a developer's own Python, which this
 * must never replace.
 */
export function shouldOfferUpdate(input: {
  /** The installed server's version; null when none is installed. */
  installed: string | null;
  /** The version this extension carries; null when it carries none. */
  wanted: string | null;
  /** SWISSARMYGRID_SERVER is set: someone is working on SwissArmyGrid itself. */
  developerOverride: boolean;
}): boolean {
  if (input.developerOverride || input.wanted === null) return false;
  return input.installed === null || isOlder(input.installed, input.wanted);
}

const RESTART_LIMIT = 3;
const RESTART_WINDOW_MS = 10 * 60 * 1000;

/**
 * May a server that died be started again? Up to three times in ten minutes, so a server that crashes on start doesn't
 * loop; after that it stays stopped and the status bar says so. `history` is when earlier automatic restarts happened.
 */
export function mayRestart(history: number[], now: number): boolean {
  return history.filter((at) => now - at < RESTART_WINDOW_MS).length < RESTART_LIMIT;
}
