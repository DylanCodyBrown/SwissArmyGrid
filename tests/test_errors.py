"""The error panel: the facts read in the kernel (client/kernel_errors.py) and the words for them
(domain/errors.py). Made-up code and data only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from swissarmygrid.client import kernel_errors
from swissarmygrid.domain import errors


def _last_error(code: str, folder: Path) -> dict[str, Any]:
    """Run made-up code that fails, as a script would, and read the error as the kernel helper does."""
    script = folder / "made_up_script.py"
    script.write_text(code, encoding="utf-8")
    try:
        exec(compile(code, str(script), "exec"), {"__name__": "__main__"})  # noqa: S102 (the test's own code)
    except BaseException as error:
        if error.__traceback__ is not None and not isinstance(error, SyntaxError):
            error.__traceback__ = error.__traceback__.tb_next  # this helper's own frame
        sys.last_exc = error  # what IPython sets after showing an error
        sys.last_value = error
    detail = json.loads(kernel_errors.detail())
    return dict(detail)


# ===== the words =====


def test_a_missing_name_offers_its_import_or_a_close_name() -> None:
    told = errors.explain(
        {
            "type": "NameError",
            "message": "name 'pd' is not defined",
            "facts": {"name": "pd", "candidates": []},
        }
    )
    assert told["fixes"][0] == {
        "kind": "import",
        "title": "Add `import pandas as pd`",
        "line": "import pandas as pd",
    }
    told = errors.explain(
        {
            "type": "NameError",
            "message": "name 'totl' is not defined",
            "facts": {"name": "totl", "candidates": ["total"]},
        }
    )
    assert {"kind": "replace", "title": "Use 'total'", "target": "totl", "text": "total"} in told["fixes"]
    assert told["fixes"][-1]["kind"] == "define"
    assert "has not run yet" in told["explanation"]


def test_a_missing_module_is_installed_by_its_package_name() -> None:
    told = errors.explain(
        {
            "type": "ModuleNotFoundError",
            "message": "No module named 'sklearn'",
            "facts": {"module": "sklearn.linear_model"},
        }
    )
    assert told["fixes"] == [
        {"kind": "install", "title": "Install 'scikit-learn' here", "package": "scikit-learn"}
    ]
    assert "'scikit-learn'" in told["explanation"]


def test_a_missing_column_lists_the_columns_and_the_close_one() -> None:
    facts = {
        "key": "'amont'",
        "table": True,
        "keys": ["order_id", "amount", "region"],
        "candidates": ["amount"],
    }
    told = errors.explain({"type": "KeyError", "message": "'amont'", "facts": facts})
    assert (
        told["explanation"]
        == "The table has no column 'amont'. Its columns are 'order_id', 'amount' and 'region'."
    )
    assert told["fixes"] == [
        {"kind": "replace", "title": "Use 'amount'", "target": "amont", "text": "amount"}
    ]


def test_none_and_files_are_said_plainly() -> None:
    told = errors.explain(
        {
            "type": "AttributeError",
            "message": "'NoneType' object has no attribute 'head'",
            "facts": {"name": "head", "is_none": True},
        }
    )
    assert told["explanation"].startswith("The value is None")
    told = errors.explain(
        {
            "type": "FileNotFoundError",
            "message": "No such file",
            "facts": {"full_path": "/data/sales.csv", "cwd": "/home/ana"},
        }
    )
    assert "/data/sales.csv" in told["explanation"] and "/home/ana" in told["explanation"]


def test_sql_errors_point_at_their_place_and_offer_the_candidates() -> None:
    message = (
        'Binder Error: Referenced column "amont" not found in FROM clause!\n'
        'Candidate bindings: "orders.amount", "orders.order_id"\n\n'
        "LINE 1: SELECT sum(amont) FROM orders\n"
        "                   ^"
    )
    where = errors.sql_position(message)
    assert where is not None
    assert (where["line"], where["col"], where["missing"]) == (1, 12, "amont")
    assert where["candidates"] == ["amount", "order_id"]
    told = errors.explain({"type": "UsageError", "message": message, "facts": {}})
    assert told["fixes"][0] == {
        "kind": "replace",
        "title": "Use 'amount'",
        "target": "amont",
        "text": "amount",
    }


def test_where_names_your_own_code() -> None:
    detail = {
        "chain": [
            {
                "type": "ZeroDivisionError",
                "message": "division by zero",
                "facts": {},
                "frames": [
                    {"where": "cell", "cell": 3, "line": 2},
                    {"where": "library", "file": "/x/pandas/core.py", "line": 9},
                ],
                "your_frame": 0,
            }
        ]
    }
    told = errors.explain_detail(detail)
    assert told["where"] == "cell [3], line 2"
    assert told["chain"][0]["explanation"].startswith("A number was divided by zero")


# ===== the facts, read in the kernel's Python =====


def test_the_failing_expression_its_columns_and_values(tmp_path: Path) -> None:
    found = _last_error(
        "rates = {'north': 2}\ndef share(region):\n    return 10 / rates[region]\n\nshare('south')\n",
        tmp_path,
    )
    last = found["chain"][-1]
    assert last["type"] == "KeyError" and last["message"] == "'south'"
    frames = last["frames"]
    assert [f["function"] for f in frames] == ["<module>", "share"]
    inner = frames[last["your_frame"]]
    assert inner["where"] == "file" and inner["line"] == 3
    assert inner["code"] == "    return 10 / rates[region]"
    assert (inner["col"], inner["end_col"]) == (17, 30)  # rates[region], 1-based as the editor counts
    assert {"name": "region", "repr": "'south'", "type": "str", "table": False} in inner["locals"]
    assert last["facts"]["keys"] == ["north"]


def test_a_chain_comes_cause_first(tmp_path: Path) -> None:
    found = _last_error(
        "try:\n    int('x')\nexcept ValueError as e:\n"
        "    raise RuntimeError('could not read the count') from e\n",
        tmp_path,
    )
    assert [e["type"] for e in found["chain"]] == ["ValueError", "RuntimeError"]
    assert found["chain"][0]["relation"] == "cause"


def test_a_name_error_finds_the_close_name(tmp_path: Path) -> None:
    found = _last_error("total = 3\nprint(totl)\n", tmp_path)
    assert found["chain"][-1]["facts"]["candidates"][:1] == ["total"]


@pytest.mark.parametrize("line", ["x = (1 +\n", "for i in range(3)\n    pass\n"])
def test_a_syntax_error_says_where_python_stopped(line: str, tmp_path: Path) -> None:
    syntax = _last_error(line, tmp_path)["chain"][-1]["syntax"]
    assert syntax["line"] >= 1 and syntax["col"] >= 1


def test_columns_count_as_the_editor_does_past_wide_characters() -> None:
    assert (
        kernel_errors._utf16_column("x = '€😀' + 1", 13) == 10
    )  # bytes 13 -> after the emoji (two UTF-16 units)


def test_a_loc_lookup_lists_the_tables_columns(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    found = _last_error(
        "import pandas as pd\norders = pd.DataFrame({'region': ['North'], 'amount': [10.5]})\n"
        "orders.loc[orders['region'] == 'North', 'amont']\n",
        tmp_path,
    )
    facts = found["chain"][-1]["facts"]
    assert facts["table"] is True and facts["candidates"] == ["amount"]
    assert [e["type"] for e in found["chain"]] == ["KeyError"]  # pandas' own inner KeyError left out
