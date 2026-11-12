/**
 * modelOffer.ts: "this notebook goes with these ERD models. Apply them?" [WIRE]
 *
 * An ERD (a relationship model: primary keys and joins) can be associated with a notebook in FI-Atlas. When the
 * notebook is opened, its Python looks the associations up and hands the models to the server (client/model_offers.py).
 * The server tells every page with a `models_offered` notice, and this module asks the user which of them to apply.
 * Apply merges the chosen ones into the current relationship model, exactly as picking one in the Relationships
 * dialog does; Skip tells the server to stop asking. Closing the dialog with the X or Escape means "not now": the
 * question comes back next time the page loads, which is what "ask me when I open the notebook" should do.
 *
 * It asks about one notebook at a time, and at most once per page load per notebook. The Open dialog keeps a
 * "Models" button on each session, so a skipped offer can still be applied later.
 *
 * ROUTING: [OFFER DIALOG]   models_offered notice, or a page that connects -> checkOffers() -> showOffer()
 *          [MODEL CHECKBOX] -> read-only, derived from the session's offer (all ticked to start)
 *          [APPLY SELECTED] -> applyOffer()    POST /processes/{id}/models/apply   {names}
 *          [SKIP]           -> skipOffer()     POST /processes/{id}/models/dismiss
 *          [CLOSED BY ANOTHER PAGE] models_handled notice -> closes this dialog
 */
import { api, onNotice, type Notice } from "./api";
import { button, h, openDialog, toast } from "./dom";

export interface OfferedModel {
  name: string;
  description: string;
  tables: string[];
  relationships: number;
}

export interface SessionOffer {
  notebook: string;
  error: string | null;
  handled: boolean;
  models: OfferedModel[];
}

export interface OfferingSession {
  id: string;
  label: string;
  models: SessionOffer | null;
}

/** The notebooks this page has already asked about, so a refresh does not ask twice. */
const asked = new Set<string>();
/** The open dialog and the session it is for; one at a time. */
let current: { id: string; close: () => void } | null = null;

/** Listen for offers and for another page answering one. Called once, by the workspace page. */
export function mountModelOffers(): void {
  onNotice((notice: Notice) => {
    if (notice.type === "models_offered") void checkOffers();
    if (notice.type === "models_handled" && current && current.id === notice.process) {
      current.close();
    }
  });
}

/** Ask about the first notebook with models waiting that this page has not asked about. */
export async function checkOffers(): Promise<void> {
  if (current) return;
  let sessions: OfferingSession[];
  try {
    sessions = (await api<{ processes: OfferingSession[] }>("/processes")).processes;
  } catch {
    return; // the server is not reachable right now; the next notice or connect asks again
  }
  const next = sessions.find((s) => s.models && !s.models.handled && s.models.models.length > 0 && !asked.has(s.id));
  if (next) showOffer(next);
}

/** The dialog, for a session that has models. `again` re-opens one that was skipped (the Open dialog's button). */
export function showOffer(session: OfferingSession, again = false): void {
  const offer = session.models;
  if (!offer || !offer.models.length || (current && !again)) return;
  asked.add(session.id);
  const notebook = session.label || offer.notebook;
  const { dialog, body, foot, close } = openDialog(`ERD models for ${notebook}`);
  current = { id: session.id, close };
  dialog.addEventListener("close", () => {
    current = null;
    void checkOffers(); // another notebook may be waiting
  });

  const boxes = offer.models.map((m) => ({ model: m, box: h("input", { type: "checkbox", checked: true, name: "offered-model", value: m.name }) }));
  body.append(
    h("p", {}, `${notebook} is associated with ${offer.models.length === 1 ? "this model" : "these models"}. Apply the ones you want to use with the tables you open.`),
    h(
      "div",
      { role: "group", "aria-label": "Associated models" },
      ...boxes.map(({ model, box }) =>
        h(
          "label",
          { class: "choice" },
          box,
          h(
            "span",
            {},
            h("strong", {}, model.name),
            model.description ? h("small", {}, model.description) : "",
            h("small", {}, `${model.tables.length} tables, ${model.relationships} joins`),
          ),
        ),
      ),
    ),
    ...(offer.error ? [h("p", { class: "hint", role: "status" }, offer.error)] : []),
  );

  const chosen = (): string[] => boxes.filter(({ box }) => (box as HTMLInputElement).checked).map(({ model }) => model.name);
  foot.append(
    button("Skip", () => void skipOffer(session.id, close), { title: "Do not apply these now, and stop asking for this notebook" }),
    button(
      "Apply selected",
      () => void applyOffer(session.id, chosen(), close),
      { class: "primary", icon: "crown", title: "Merge the ticked models into the current relationship model" },
    ),
  );
}

async function applyOffer(id: string, names: string[], close: () => void): Promise<void> {
  if (!names.length) {
    toast("Tick at least one model, or choose Skip.", "error");
    return;
  }
  try {
    const result = await api<{ applied: string[] }>(`/processes/${encodeURIComponent(id)}/models/apply`, "POST", { names });
    toast(`Applied ${result.applied.length === 1 ? result.applied[0] : `${result.applied.length} models`} to the relationship model.`);
    close();
  } catch (e) {
    toast(`Could not apply the models: ${(e as Error).message}`, "error");
  }
}

async function skipOffer(id: string, close: () => void): Promise<void> {
  try {
    await api(`/processes/${encodeURIComponent(id)}/models/dismiss`, "POST", {});
  } catch {
    // Already answered from another page; closing is still right.
  }
  close();
}
