"""Recipes: a grid's steps and settings saved to a small file, with no rows (plan section 7).

Saving is an explicit user action ("Save recipe"), because step parameters hold the values the
user typed (a filter on 'Acme', say), and the default posture writes nothing to disk. When a
dataset is pushed and gets its first grid, a saved recipe for that dataset name is applied
automatically; if a column it needs is missing, compilation marks the first broken step and
the grid stops there (shown in the steps panel).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swissarmygrid.domain.datasets import validate_name
from swissarmygrid.domain.steps import Step, ViewSettings, steps_from_json


def _path(folder: Path, dataset: str) -> Path:
    return folder / f"{validate_name(dataset)}.json"


def save_recipe(folder: Path, dataset: str, steps: list[Step], settings: ViewSettings) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = _path(folder, dataset)
    body: dict[str, Any] = {
        "dataset": dataset,
        "settings": settings.to_json(),
        "steps": [s.to_json() for s in steps if not s.params.get("header")],
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def load_recipe(folder: Path, dataset: str) -> tuple[list[Step], ViewSettings] | None:
    path = _path(folder, dataset)
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):  # edited by hand into something else: ignore it
            return None
        return steps_from_json(body.get("steps", [])), ViewSettings.from_json(body.get("settings"))
    except (ValueError, OSError):
        return None


def delete_recipe(folder: Path, dataset: str) -> bool:
    path = _path(folder, dataset)
    if path.exists():
        path.unlink()
        return True
    return False
