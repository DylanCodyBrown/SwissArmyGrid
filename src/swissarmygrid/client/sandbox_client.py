"""`sag.sandbox(thing)`: put anything visual from Python into the Sandbox, as a card of no table.

    sag.sandbox(fig)                          # matplotlib or seaborn: a PNG picture
    sag.sandbox(plotly_fig, "Sales by month") # Plotly: drawn live, in the theme
    sag.sandbox(altair_chart)                 # Altair: drawn as Vega-Lite (its data inline)
    sag.sandbox(digraph)                      # graphviz: drawn from its DOT text
    sag.sandbox(frame)                        # a DataFrame or Arrow table: its first 100 rows
    sag.sandbox("## Findings\\n...")           # Markdown text: a note
    sag.sandbox("chart.png")                  # a file: a picture, .md note, .dot, .mmd, .vl.json
    sag.sandbox(text, kind="mermaid")         # Mermaid, DOT or ECharts text, named

`key="..."` names the card, so running the cell again replaces it in place (its size and position
stay) instead of adding another. `size=(w, h)` is 2 to 12 columns by 3 to 30 rows. Returns the card's
id. It duck-types what it is given, so nothing here imports matplotlib, Plotly or pandas: standard
library only, like the rest of the client.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import io
import json
import math
import os
from pathlib import Path
from typing import Any

from swissarmygrid.client.blades_client import _call

_ROWS: int = 100
_KIND_NAMES: dict[str, str] = {
    "markdown": "note",
    "md": "note",
    "note": "note",
    "mermaid": "diagram",
    "diagram": "diagram",
    "dot": "graphviz",
    "graphviz": "graphviz",
    "plotly": "plotly",
    "echarts": "echarts",
    "vegalite": "vegalite",
    "vega-lite": "vegalite",
    "altair": "vegalite",
    "image": "image",
    "table": "table",
}
_PICTURES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
_TEXT_FILES: dict[str, str] = {
    ".md": "note",
    ".markdown": "note",
    ".mmd": "diagram",
    ".mermaid": "diagram",
    ".dot": "graphviz",
    ".gv": "graphviz",
}


def sandbox(
    thing: object,
    title: str | None = None,
    *,
    key: str | None = None,
    size: tuple[int, int] | None = None,
    kind: str | None = None,
) -> str:
    """Add `thing` to the Sandbox (or replace the card named `key`). Returns the card's id."""
    card = card_for(thing, kind)
    if title is not None:
        card["title"] = title
    card.setdefault("title", "")
    if key is not None:
        card["key"] = key
    if size is not None:
        w, h = size
        card["size"] = {"w": int(w), "h": int(h)}
    card["origin"] = "python"
    answer = _call("POST", "/cards", json.dumps(card).encode("utf-8"))
    return str(answer["card"])


def card_for(thing: object, kind: str | None = None) -> dict[str, Any]:
    """The card for `thing`: {"kind", "body"} or a table's {"kind", "columns", "rows"}. No server."""
    named = _kind(kind)
    if isinstance(thing, Path) or (isinstance(thing, str) and _is_file(thing)):
        return _from_file(Path(thing), named)
    if isinstance(thing, bytes | bytearray | memoryview):
        return {"kind": "image", "body": _data_url(bytes(thing))}
    if isinstance(thing, str):
        return {"kind": named or "note", "body": thing}
    if isinstance(thing, dict):
        return _from_dict(thing, named)
    module = type(thing).__module__.split(".")[0]
    if hasattr(thing, "to_plotly_json") and hasattr(thing, "to_json"):
        return {"kind": "plotly", "body": thing.to_json()}
    if module == "altair" and hasattr(thing, "to_dict"):
        return {"kind": "vegalite", "body": json.dumps(thing.to_dict(), default=_plain)}
    if module == "graphviz" and isinstance(getattr(thing, "source", None), str):
        return {"kind": "graphviz", "body": thing.source}  # type: ignore[attr-defined]
    figure = _figure(thing)
    if figure is not None:
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=144, bbox_inches="tight")
        return {"kind": "image", "body": _data_url(buffer.getvalue())}
    if module == "PIL" and hasattr(thing, "save"):
        buffer = io.BytesIO()
        thing.save(buffer, format="PNG")
        return {"kind": "image", "body": _data_url(buffer.getvalue())}
    table = _table(thing)
    if table is not None:
        return table
    raise TypeError(
        f"Can't put a {type(thing).__name__} in the Sandbox. It takes a matplotlib, seaborn, Plotly, "
        "Altair or graphviz figure, a PIL image, picture bytes, a DataFrame or Arrow table, Markdown "
        "text, a file path, or Mermaid, DOT or ECharts text with kind=."
    )


def _kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    named = _KIND_NAMES.get(kind.casefold())
    if named is None:
        raise ValueError(f"kind is one of {', '.join(sorted(_KIND_NAMES))}.")
    return named


def _is_file(text: str) -> bool:
    return "\n" not in text and len(text) < 1_000 and os.path.isfile(text)


def _from_file(path: Path, named: str | None) -> dict[str, Any]:
    suffix = path.suffix.casefold()
    if suffix in _PICTURES and named in (None, "image"):
        return {"kind": "image", "body": _data_url(path.read_bytes(), _PICTURES[suffix])}
    text = path.read_text(encoding="utf-8")
    if named:
        return {"kind": named, "body": text}
    if suffix in _TEXT_FILES:
        return {"kind": _TEXT_FILES[suffix], "body": text}
    if path.name.casefold().endswith((".vl.json", ".vega-lite.json")):
        return {"kind": "vegalite", "body": text}
    if suffix == ".json":
        return _from_dict(json.loads(text), None)
    raise ValueError(
        f"Name what {path.name} is with kind= (markdown, mermaid, dot, plotly, echarts, vegalite)."
    )


def _from_dict(spec: dict[str, Any], named: str | None) -> dict[str, Any]:
    if named is None:
        schema = str(spec.get("$schema", ""))
        if "vega-lite" in schema:
            named = "vegalite"
        elif "data" in spec and isinstance(spec["data"], list):
            named = "plotly"
        else:
            raise ValueError("Name what this dict is with kind= (plotly, echarts or vegalite).")
    return {"kind": named, "body": json.dumps(spec, default=_plain)}


def _figure(thing: object) -> Any:
    """A matplotlib figure, or the figure behind an Axes or a seaborn grid."""
    for candidate in (thing, getattr(thing, "figure", None), getattr(thing, "fig", None)):
        if candidate is not None and callable(getattr(candidate, "savefig", None)):
            return candidate
    return None


def _sniff(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip()[:5] in (b"<svg ", b"<?xml") or data.lstrip().startswith(b"<svg"):
        return "image/svg+xml"
    raise ValueError("These bytes are not a PNG, JPEG, GIF, WebP or SVG picture.")


def _data_url(data: bytes, media: str | None = None) -> str:
    return f"data:{media or _sniff(data)};base64,{base64.b64encode(data).decode('ascii')}"


def _table(thing: object) -> dict[str, Any] | None:
    """The first 100 rows of a pandas or polars DataFrame, an Arrow table, or a list of dicts."""
    module = type(thing).__module__.split(".")[0]
    columns: list[str]
    rows: list[list[Any]]
    if module == "pandas" and hasattr(thing, "head") and hasattr(thing, "columns"):
        frame: Any = thing
        total = len(frame)
        head = frame.head(_ROWS)
        columns = [str(c) for c in head.columns]
        rows = [list(r) for r in head.itertuples(index=False, name=None)]
    elif module == "polars" and hasattr(thing, "head") and hasattr(thing, "rows"):
        frame = thing
        total = frame.height
        columns = [str(c) for c in frame.columns]
        rows = [list(r) for r in frame.head(_ROWS).rows()]
    elif module == "pyarrow" and hasattr(thing, "slice") and hasattr(thing, "to_pylist"):
        table: Any = thing
        total = table.num_rows
        columns = [str(c) for c in table.column_names]
        rows = [[r[c] for c in table.column_names] for r in table.slice(0, _ROWS).to_pylist()]
    elif isinstance(thing, list) and thing and all(isinstance(r, dict) for r in thing):
        total = len(thing)
        columns = list(dict.fromkeys(str(k) for r in thing for k in r))
        rows = [[r.get(c) for c in columns] for r in thing[:_ROWS]]
    else:
        return None
    return {
        "kind": "table",
        "columns": columns,
        "rows": [[_cell(v) for v in r] for r in rows],
        "capped": total > _ROWS,
    }


def _cell(value: Any) -> Any:
    """A cell as JSON: numbers and text as they are, blanks as null, dates as ISO text."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, _dt.date | _dt.time):
        return value.isoformat()
    item = getattr(value, "item", None)  # numpy scalars
    if callable(item):
        try:
            return _cell(item())
        except (TypeError, ValueError):
            pass
    text = str(value)
    return None if text in ("NaT", "<NA>", "nan") else text[:1_000]


def _plain(value: Any) -> Any:
    """json.dumps' fallback for dates and numpy values inside a spec."""
    return _cell(value)
