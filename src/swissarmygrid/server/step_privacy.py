"""What an agent sees of a grid's steps at privacy level 0: their shape, never their values.

A step's values are text the user typed (a filter's value, a categorize rule, a SQL step's query, the
"+" tab's query), and that text is often client data. Level 0 promises counts and column names only,
so every route an agent can read steps through (`tabs_get`, the step commands' answers,
`agent_view_state`, `agent_context`, an export's provenance) shows the same redacted form: the step's
id, kind and on/off state, and the params that only name columns or shape the step (a filter's column
and op, sort keys, kept columns, group keys and aggregates, a bucket's numeric edges). Every other
param is replaced by `HIDDEN`, so the agent sees that a value is there without reading it.

An agent changes steps without reading their values back: the server applies a change to one step by
its id or position (POST /grids/{id}/step-ops). For a client that still reads the list and posts it
back whole, `restore_hidden` puts the user's real params back into any step posted exactly as the
redacted form of the step with the same id, so a round trip never overwrites a value with the marker.
"""

from __future__ import annotations

from typing import Any

from swissarmygrid.engine.describe import describe_step
from swissarmygrid.engine.steps import Step

HIDDEN: str = "<hidden at privacy level 0>"

# Per kind: the params that name columns or shape the step, never a value the user typed.
_SHAPE_PARAMS: dict[str, frozenset[str]] = {
    "filter": frozenset({"column", "op", "from_chart"}),
    "sort": frozenset({"keys", "header"}),
    "columns": frozenset({"keep"}),
    "rename": frozenset({"mapping"}),
    "group_by": frozenset({"keys", "aggregates"}),
    "distinct": frozenset(),
    "limit": frozenset({"n"}),
    "transform": frozenset({"fn", "columns"}),
    # Numeric edges are numbers, which level 0 shows (as min and max of a number column); the label
    # for values outside every bucket is text the user typed.
    "bucket": frozenset({"column", "as", "mode", "unit", "edges", "width", "low", "high", "right_closed"}),
    "categorize": frozenset({"column", "as"}),
    "sql": frozenset(),
}


def redact_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """`params` with every value the user typed replaced by HIDDEN."""
    keep = _SHAPE_PARAMS.get(kind, frozenset())
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in keep:
            out[key] = value
        elif kind == "categorize" and key == "rules" and isinstance(value, list):
            # How many rules and of which type: their values and labels are the user's text.
            out[key] = [
                {k: (v if k == "type" else HIDDEN) for k, v in rule.items()}
                if isinstance(rule, dict)
                else HIDDEN
                for rule in value
            ]
        else:
            out[key] = HIDDEN
    return out


def redact_step(step: Step) -> dict[str, Any]:
    """The step as an agent at level 0 sees it, with a description that names no value."""
    params = redact_params(step.kind, step.params)
    shown = Step(kind=step.kind, params=params, enabled=step.enabled, id=step.id)
    try:
        description = describe_step(shown)
    except Exception:  # a malformed step the user saved: its kind is enough
        description = step.kind
    return shown.to_json() | {"description": description}


def redacted_descriptions(steps: list[Step]) -> list[str]:
    """`describe_steps` for level 0 (an export's provenance)."""
    return [
        f"{i}. {redact_step(s)['description']}{'' if s.enabled else ' (off)'}"
        for i, s in enumerate(steps, start=1)
    ]


def restore_hidden(posted: list[Step], current: list[Step]) -> list[Step]:
    """`posted` with each step that is the redacted form of the current step of the same id
    replaced by that current step, so values hidden from the agent survive its round trip."""
    by_id = {s.id: s for s in current}
    restored: list[Step] = []
    for step in posted:
        original = by_id.get(step.id)
        if (
            original is not None
            and step.kind == original.kind
            and step.params == redact_params(original.kind, original.params)
            and step.params != original.params
        ):
            restored.append(
                Step(kind=original.kind, params=original.params, enabled=step.enabled, id=step.id)
            )
        else:
            restored.append(step)
    return restored


def mentions_hidden(value: Any) -> bool:
    """True when `value` (a step's params) still holds the HIDDEN marker somewhere."""
    if isinstance(value, str):
        return value == HIDDEN
    if isinstance(value, dict):
        return any(mentions_hidden(v) for v in value.values())
    if isinstance(value, list):
        return any(mentions_hidden(v) for v in value)
    return False
