"""Steps in plain language: "Keep rows where city equals 'Oslo'".

Used by the steps panel, the Excel "Provenance" sheet, Parquet metadata and the comment headers
of generated code, so a step reads the same everywhere. Never includes cell values beyond what
the user typed into the step itself.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.engine.steps import Step, ViewSettings, aggregate_name

_FILTER_WORDS: dict[str, str] = {
    "eq": "equals",
    "ne": "does not equal",
    "in": "is one of",
    "not_in": "is none of",
    "blank": "is blank",
    "not_blank": "is not blank",
    "contains": "contains",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "lt": "is less than",
    "gt": "is greater than",
    "between": "is between",
    "before": "is before",
    "after": "is after",
    "date_between": "is between",
    "true": "is true",
    "false": "is false",
}


def _v(value: Any) -> str:
    return repr(value) if isinstance(value, str) else str(value)


def describe_step(step: Step) -> str:
    """One step in words. A step whose params have the wrong shape (saved as sent, so that its
    error can show) is described by its kind alone, rather than failing every read of the grid."""
    try:
        return _describe_step(step)
    except (AttributeError, TypeError, ValueError):
        return step.kind


def _describe_step(step: Step) -> str:
    p = step.params
    kind = step.kind
    if kind == "filter":
        words = _FILTER_WORDS.get(str(p.get("op")), str(p.get("op")))
        column = p.get("column")
        op = p.get("op")
        if op in ("in", "not_in"):
            values = ", ".join(_v(v) for v in p.get("values", []))
            return f"Keep rows where {column} {words} [{values}]"
        if op in ("between", "date_between"):
            return f"Keep rows where {column} {words} {_v(p.get('low'))} and {_v(p.get('high'))}"
        if op in ("blank", "not_blank", "true", "false"):
            return f"Keep rows where {column} {words}"
        return f"Keep rows where {column} {words} {_v(p.get('value'))}"
    if kind == "sort":
        keys = ", ".join(f"{c} {'descending' if d == 'desc' else 'ascending'}" for c, d in p.get("keys", []))
        return f"Sort by {keys}"
    if kind == "columns":
        return "Keep columns " + ", ".join(p.get("keep", []))
    if kind == "rename":
        return "Rename " + ", ".join(f"{a} to {b}" for a, b in p.get("mapping", {}).items())
    if kind == "group_by":
        keys = ", ".join(p.get("keys", [])) or "everything"
        aggs = ", ".join(aggregate_name(a) for a in p.get("aggregates", []))
        return f"Group by {keys}: {aggs}"
    if kind == "distinct":
        return "Keep distinct rows"
    if kind == "limit":
        return f"Keep the first {p.get('n')} rows"
    if kind == "transform":
        columns = p.get("columns")
        target = "every text column" if columns == "*text" else ", ".join(columns or [])
        verb = {
            "trim_both": "Trim spaces from both ends of",
            "trim_left": "Trim leading spaces from",
            "trim_right": "Trim trailing spaces from",
            "upper": "Upper-case",
            "lower": "Lower-case",
        }.get(str(p.get("fn")), str(p.get("fn")))
        return f"{verb} {target}"
    if kind == "bucket":
        if p.get("mode") == "date":
            return f"Bucket {p.get('column')} by {p.get('unit')} into {p.get('as')}"
        return f"Bucket {p.get('column')} into {p.get('as')}"
    if kind == "categorize":
        return f"Categorize {p.get('column')} into {p.get('as')} ({len(p.get('rules', []))} rules)"
    if kind == "sql":
        text = " ".join(str(p.get("sql", "")).split())
        return f"SQL: {text if len(text) <= 120 else text[:117] + '...'}"
    return kind


def describe_steps(steps: list[Step]) -> list[str]:
    return [f"{i}. {describe_step(s)}{'' if s.enabled else ' (off)'}" for i, s in enumerate(steps, start=1)]


def describe_settings(settings: ViewSettings) -> str:
    on = [
        name
        for name, value in (
            ("case insensitive", not settings.case_sensitive),
            ("wildcards", settings.wildcards),
            ("trim spaces", settings.trim_spaces),
            ("empty text is blank", settings.empty_is_blank),
        )
        if value
    ]
    return ", ".join(on) if on else "defaults"
