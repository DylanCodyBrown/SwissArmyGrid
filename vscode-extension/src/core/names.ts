// names.ts -- the data files the push button takes, and the dataset name a file (or one sheet of it) gets.

/** What the server reads (engine/loaders.py SUFFIXES). Keep in step with the `when` clauses in package.json. */
export const DATA_FILES: readonly string[] = [
  ".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson", ".parquet", ".arrow", ".feather", ".xlsx",
];

export function isDataFile(fileName: string): boolean {
  const dot = fileName.lastIndexOf(".");
  return dot >= 0 && DATA_FILES.includes(fileName.slice(dot).toLowerCase());
}

/**
 * The dataset name for a file, and for one sheet of a workbook: the server takes 1 to 64 letters, digits, _ or -.
 * "Q3 sales (final).csv" -> "Q3_sales_final"; sheet "North" of "book.xlsx" -> "book_North". Pushing the same file
 * again lands on the same name, so its grid refreshes in place.
 */
export function datasetName(fileName: string, sheet?: string): string {
  const stem = fileName.replace(/\.[^.]*$/, "");
  const raw = sheet === undefined ? stem : `${stem}_${sheet}`;
  return raw.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64) || "data";
}
