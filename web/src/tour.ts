/**
 * The tour: what every word in SwissArmyGrid means, for a first-time user, in as few words as
 * possible. The question mark in the Application row opens it; until it has been opened once, a
 * small "New here?" bubble sits beside it in the row (inline, so it never covers anything).
 *
 *   [TOUR]            Application row -> openTour()
 *   a card per word   lights up the real control (a spotlight), or sits in the middle when that
 *                     control isn't on screen (no grid open, another tab showing)
 *   the game          five "which word is this?" rounds, then a score drawn as opened blades
 *
 * Keys: Right or Enter = next, Left = back, Escape = close. Nothing is sent anywhere; the only thing
 * kept is "seen" in this browser (dom.remember).
 */

import { button, h, icon, recall, remember } from "./dom";

interface Stop {
  icon: string;
  word: string;
  line: string;
  /** A CSS selector for the control to light up; the first visible match wins. */
  target?: string;
  /** Where to find it, when the lit-up control has another name. Not in the game: it would give it away. */
  where?: string;
  /** A line of code, when the word is something you type. */
  code?: string;
}

const BLADES_BUTTON = '.app-row [aria-label="Blades and catalog"]';

export const STOPS: readonly Stop[] = [
  { icon: "star", word: "Hi!", line: "Your Python tables, live in the browser. A minute of words, then a game." },
  { icon: "rows", word: "Grid", line: "A table you pushed from Python. One tab each.", target: ".tabs-row .tab-item:not(.tab-app)", code: "sag.push(orders)" },
  { icon: "filter", word: "Ribbon", line: "Filter, sort, clean. Your data never changes.", target: ".ribbon" },
  { icon: "steps", word: "Steps", line: "Every click is a step. Undo any one.", target: ".steps-strip" },
  { icon: "sql", word: "Copy as SQL", line: "Your steps as one DuckDB query, checked before you copy.", target: '.steps-strip [aria-label="Copy as SQL"]' },
  { icon: "profile", word: "Profile", line: "A column at a glance: blanks, top values, spread.", target: '.ribbon [aria-label="Profile"]' },
  { icon: "fit", word: "Analyze", line: "Odds, predictors, fit tests and outliers that hand off to each other.", target: '.ribbon [aria-label="Analyze"]' },
  { icon: "fit", word: "Fit test", line: "Do a column's values spread as expected? Chi-square says.", target: '.ribbon [aria-label="Fit test…"]' },
  { icon: "outliers", word: "Outliers", line: "Values far from the rest, or seen too often or too rarely.", target: '.ribbon [aria-label="Outliers…"]' },
  { icon: "columns", word: "Compare", line: "Drag a tab onto another: two tables, row by row.", target: ".tabs-row" },
  { icon: "zoom", word: "Key Finder", line: "Finds the columns two tables share.", target: '.ribbon [aria-label="Key Finder"]' },
  { icon: "crown", word: "Tang", line: "Saved joins between tables. Run one to test it.", where: "Relationships", target: '.ribbon [aria-label="Relationships"]' },
  { icon: "image", word: "Sandbox", line: "Charts, notes and diagrams on one board.", target: ".tab-agent" },
  { icon: "key", word: "Blade", line: "Saved code that loads data: a database, a file.", where: "Blades and catalog", target: BLADES_BUTTON, code: 'conn = sag.blade("Warehouse")' },
  { icon: "folder", word: "Catalog", line: "Every table and column your blades reach. Search it.", where: "Blades and catalog", target: BLADES_BUTTON },
  { icon: "group", word: "Pack", line: "Blades and tangs your team shares on GitHub.", where: "Blades and catalog, then Packs", target: BLADES_BUTTON },
  { icon: "code", word: "Notebook", line: "Python and SQL cells, in a tab of its own.", target: '.app-row [aria-label="Notebook"]' },
  { icon: "agent", word: "Agent", line: "Your AI helper. Sees what you see, asks before it exports.", where: "Settings", target: '.app-row [aria-label="Settings"]' },
];

const ROUNDS = 5;
const SEEN = "tour.seen";

/** The Application row's question mark, with the "New here?" bubble until the tour is opened once. */
export function tourButton(): HTMLElement {
  const btn = button("Tour", () => openTour(), {
    icon: "help",
    class: "ribbon-btn tour-btn",
    title: "What everything means: a one-minute tour",
  });
  btn.setAttribute("aria-haspopup", "dialog");
  if (recall(SEEN)) return btn;
  const bubble = h(
    "span",
    { class: "tour-bubble" },
    h("button", { type: "button", class: "tour-bubble-go", onclick: () => openTour() }, "New here? Take the tour"),
    button("Not now", () => markSeen(), { icon: "close", iconOnly: true, class: "ghost" }),
  );
  btn.classList.add("tour-new");
  return h("span", { class: "tour-slot" }, btn, bubble);
}

function markSeen(): void {
  remember(SEEN, "1");
  document.querySelectorAll(".tour-bubble").forEach((b) => b.remove());
  document.querySelectorAll(".tour-new").forEach((b) => b.classList.remove("tour-new"));
}

/** The first visible element for a selector, or null (then the card sits in the middle). */
function visible(selector: string | undefined): HTMLElement | null {
  if (!selector) return null;
  for (const el of document.querySelectorAll<HTMLElement>(selector)) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight) return el;
  }
  return null;
}

function shuffled<T>(items: readonly T[]): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j] as T, out[i] as T];
  }
  return out;
}

export function openTour(): void {
  markSeen();
  document.querySelector(".tour")?.remove();
  const before = document.activeElement as HTMLElement | null;
  const spot = h("div", { class: "tour-spot", "aria-hidden": "true" });
  const card = h("div", { class: "tour-card" });
  const root = h("div", { class: "tour", role: "dialog", "aria-modal": "true", "aria-label": "Tour" }, spot, card);
  let index = 0;
  let place: () => void = () => undefined;

  const close = (): void => {
    root.remove();
    window.removeEventListener("resize", onResize);
    before?.focus();
  };
  const onResize = (): void => place();
  window.addEventListener("resize", onResize);
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
    else if (event.key === "ArrowRight" && index < STOPS.length) show(index + 1);
    else if (event.key === "ArrowLeft" && index > 0 && index <= STOPS.length) show(index - 1);
    else return;
    event.preventDefault();
  });
  // A click on the dimmed page closes nothing: an accidental click shouldn't lose your place.

  /** Light up `target` (or nothing) and put the card beside it, inside the window. */
  const aim = (target: HTMLElement | null): void => {
    place = () => {
      const gutter = 16;
      const width = Math.min(360, window.innerWidth - 2 * gutter);
      card.style.width = `${width}px`;
      if (!target) {
        root.classList.add("tour-dim");
        spot.hidden = true;
        card.style.left = `${(window.innerWidth - width) / 2}px`;
        card.style.top = `${Math.max(gutter, (window.innerHeight - card.offsetHeight) / 2)}px`;
        return;
      }
      root.classList.remove("tour-dim");
      spot.hidden = false;
      const r = target.getBoundingClientRect();
      const pad = 6;
      Object.assign(spot.style, {
        left: `${r.left - pad}px`,
        top: `${r.top - pad}px`,
        width: `${r.width + 2 * pad}px`,
        height: `${r.height + 2 * pad}px`,
      });
      const below = r.bottom + pad + 12;
      const fitsBelow = below + card.offsetHeight + gutter <= window.innerHeight;
      const top = fitsBelow ? below : Math.max(gutter, r.top - pad - 12 - card.offsetHeight);
      const left = Math.min(Math.max(gutter, r.left + r.width / 2 - width / 2), window.innerWidth - width - gutter);
      card.style.left = `${left}px`;
      card.style.top = `${top}px`;
    };
    place();
  };

  const dots = (at: number): HTMLElement =>
    h(
      "div",
      { class: "tour-dots", "aria-label": `${at + 1} of ${STOPS.length}` },
      ...STOPS.map((_, i) => h("span", { class: `tour-dot ${i < at ? "done" : i === at ? "here" : ""}` })),
    );

  const show = (at: number): void => {
    index = at;
    if (at >= STOPS.length) return game();
    const stop = STOPS[at] as Stop;
    const last = at === STOPS.length - 1;
    const next = button(last ? "Play" : "Next", () => show(at + 1), { icon: last ? "play" : "expand", class: "primary" });
    card.replaceChildren(
      h("div", { class: "tour-icon" }, icon(stop.icon)),
      h("h2", { class: "tour-word" }, stop.word),
      h("p", { class: "tour-line" }, stop.line),
      stop.where ? h("p", { class: "tour-where" }, icon("expand"), stop.where) : "",
      stop.code ? h("code", { class: "tour-code" }, stop.code) : "",
      dots(at),
      h(
        "div",
        { class: "tour-actions" },
        button("Skip", close, { class: "ghost" }),
        at > 0 ? button("Back", () => show(at - 1), { icon: "back", class: "ghost" }) : null,
        next,
      ),
    );
    aim(visible(stop.target));
    next.focus();
  };

  /** Five rounds of "which word is this?": the meaning, three words to pick from. */
  const game = (): void => {
    const rounds = shuffled(STOPS.slice(1)).slice(0, ROUNDS);
    let round = 0;
    let score = 0;
    const play = (): void => {
      if (round >= rounds.length) return finish();
      const answer = rounds[round] as Stop;
      const others = shuffled(STOPS.slice(1).filter((s) => s !== answer)).slice(0, 2);
      const choices = shuffled([answer, ...others]);
      const buttons = choices.map((choice) =>
        h(
          "button",
          {
            type: "button",
            class: "tour-choice",
            onclick: () => {
              buttons.forEach((b) => (b.disabled = true));
              const right = choice === answer;
              if (right) score++;
              buttons[choices.indexOf(answer)]?.classList.add("right");
              if (!right) buttons[choices.indexOf(choice)]?.classList.add("wrong");
              round++;
              window.setTimeout(play, right ? 650 : 1300);
            },
          },
          icon(choice.icon),
          h("span", {}, choice.word),
        ),
      );
      card.replaceChildren(
        h("p", { class: "tour-kicker" }, `Round ${round + 1} of ${rounds.length}`),
        h("p", { class: "tour-clue" }, answer.line),
        h("div", { class: "tour-choices" }, ...buttons),
        h("div", { class: "tour-actions" }, button("Skip", finish, { class: "ghost" })),
      );
      aim(null);
      buttons[0]?.focus();
    };
    const finish = (): void => {
      const cheer = score === rounds.length ? "Sharp!" : score >= 3 ? "Nicely done." : "You'll pick it up fast.";
      const knife = h(
        "div",
        { class: "tour-knife", "aria-label": `${score} of ${rounds.length}` },
        ...rounds.map((_, i) => h("span", { class: `tour-blade ${i < score ? "open" : ""}`, style: { "--i": String(i) } })),
        h("span", { class: "tour-handle" }),
      );
      const done = button("Start", close, { icon: "check", class: "primary" });
      card.replaceChildren(
        knife,
        h("h2", { class: "tour-word" }, `${score} of ${rounds.length}. ${cheer}`),
        h("p", { class: "tour-line" }, "Push a table from any Python to begin:"),
        h("code", { class: "tour-code" }, "import swissarmygrid as sag\nsag.push(df)"),
        h("div", { class: "tour-actions" }, button("Again", () => show(0), { icon: "back", class: "ghost" }), done),
      );
      aim(null);
      done.focus();
    };
    play();
  };

  document.body.append(root);
  show(0);
}
