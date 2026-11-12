// SHARED: used by gridView (key marks in the headers), compareDialog and keyFinder (known pairs,
// crowned) and relationshipsDialog — the current relationship model, as the server holds it.
/**
 * relationships.ts: the page's copy of the current relationship model. [STATE + FUNCTION]
 *
 * The server keeps one model (routes_models.py): each table's primary key and the joins between
 * tables. This module fetches it, refetches on the relationships_updated notice, and answers the
 * two questions the rest of the page asks: is this column a table's key, and do these two tables
 * have a known join (and on which columns)?
 */
import { api } from "./api";

export interface RelationshipModel {
  name: string;
  description: string;
  tables: Record<string, { primary_key?: string[]; source?: string }>;
  relationships: { from: string; from_columns: string[]; to: string; to_columns: string[]; kind: string; note?: string }[];
}

let model: RelationshipModel = { name: "current", description: "", tables: {}, relationships: [] };
const listeners: (() => void)[] = [];

export function currentModel(): RelationshipModel {
  return model;
}

/** Subscribe to model changes; returns the unsubscribe (a grid view calls it when it closes). */
export function onRelationshipsChange(listener: () => void): () => void {
  listeners.push(listener);
  return () => {
    const at = listeners.indexOf(listener);
    if (at >= 0) listeners.splice(at, 1);
  };
}

/** Fetch the model again (on load, and when the server says it changed). */
export async function loadRelationships(): Promise<void> {
  try {
    model = await api<RelationshipModel>("/relationships");
  } catch {
    return; // keep what we have
  }
  for (const listener of [...listeners]) listener();
}

export function primaryKey(dataset: string): string[] {
  return model.tables[dataset]?.primary_key ?? [];
}

/** The known join between two tables as [left column, right column] pairs; [] when none. */
export function knownPairs(left: string, right: string): [string, string][] {
  for (const r of model.relationships) {
    if (r.from === left && r.to === right) return r.from_columns.map((c, i) => [c, r.to_columns[i]!]);
    if (r.from === right && r.to === left) return r.to_columns.map((c, i) => [c, r.from_columns[i]!]);
  }
  // The same table on both sides (two copies being compared): its primary key lines them up.
  if (left === right) return primaryKey(left).map((c) => [c, c]);
  return [];
}

export function isKnownPair(left: string, right: string, leftColumn: string, rightColumn: string): boolean {
  return knownPairs(left, right).some(([a, b]) => a === leftColumn && b === rightColumn);
}
