"""Recipe files edited by hand, and model notes that would break the ERD. Made-up data only."""

from __future__ import annotations

from pathlib import Path

import pytest

from swissarmygrid.engine.recipes import load_recipe
from swissarmygrid.engine.relationships import mermaid_erd, parse_model


@pytest.mark.parametrize("body", ["[]", '"text"', "3", '{"steps": {"kind": "sort"}}', "{", '{"settings": 1}'])
def test_a_broken_recipe_file_is_ignored(tmp_path: Path, body: str) -> None:
    # A file that is JSON but not an object raised AttributeError, which failed the push that
    # tried to apply it.
    (tmp_path / "orders.json").write_text(body, encoding="utf-8")
    assert load_recipe(tmp_path, "orders") is None


def test_a_note_with_line_breaks_stays_on_one_mermaid_line() -> None:
    model = parse_model(
        {
            "tables": {"a": {"primary_key": ["id"]}, "b": {"primary_key": ["id"]}},
            "relationships": [
                {
                    "from": "a",
                    "from_columns": ["b_id"],
                    "to": "b",
                    "to_columns": ["id"],
                    "note": 'x\n"y"\r\nz',
                }
            ],
        }
    )
    lines = mermaid_erd(model, {}).splitlines()
    assert lines[-1] == "    a }o--|| b : \"x 'y' z\""
