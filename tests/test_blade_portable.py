"""Portable blades: a blade as a plain Python file with a header, written and read back exactly."""

from __future__ import annotations

import ast
from typing import Any

import pytest

from swissarmygrid.domain.blades import PortableError, read_blade, read_blades, write_blade
from swissarmygrid.server.blade_templates import TEMPLATES

SQL_BLADE: dict[str, Any] = {
    "name": "Warehouse",
    "kind": "sql",
    "dialect": "sqlserver",
    "variable": "conn",
    "code": 'import pyodbc\n\nconn = pyodbc.connect("Server=my-server;PWD=" + secret("password"))\n',
    "catalog": {
        "language": "sql",
        "command": "SELECT table_name, column_name\nFROM information_schema.columns",
    },
}


def test_a_blade_is_a_python_file_with_a_header() -> None:
    text = write_blade(SQL_BLADE)
    assert text.startswith("# /// swissarmygrid-blade\n# format = 1\n")
    assert '# name = "Warehouse"' in text and '# secrets = ["password"]' in text
    assert text.endswith(SQL_BLADE["code"])
    ast.parse(text)  # the whole file is valid Python: it opens in any editor, diffs in Git
    assert read_blade(text) == SQL_BLADE | {"template": None}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_every_starter_round_trips(template: Any) -> None:
    blade = {
        "name": template.label.replace("/", "-")[:64].strip(),
        "kind": template.kind,
        "dialect": template.dialect,
        "variable": template.variable,
        "code": template.code + "\n",
        "catalog": template.to_json()["catalog"],
        "vault_mapping": template.to_json()["vault_mapping"],
        "template": template.id,
    }
    back = read_blade(write_blade(blade))
    for key in ("name", "kind", "variable", "code", "template"):
        assert back[key] == blade[key], key
    if template.kind == "sql":
        assert back["dialect"] == template.dialect
    if blade["catalog"]:
        assert back["catalog"] == blade["catalog"]
    if template.kind == "vault":
        assert back["vault_mapping"] == blade["vault_mapping"]


def test_vault_and_secret_blades() -> None:
    vault = {
        "name": "Azure vault",
        "kind": "vault",
        "variable": "get",
        "code": "def get(name):\n    return name\n",
        "vault_mapping": [{"env": "WAREHOUSE_PASSWORD", "secret": "warehouse-password"}],
    }
    text = write_blade(vault)
    assert '# WAREHOUSE_PASSWORD = "warehouse-password"' in text
    assert read_blade(text)["vault_mapping"] == vault["vault_mapping"]
    secret = {
        "name": "Warehouse login",
        "kind": "secret",
        "env_names": ["WAREHOUSE_USER", "WAREHOUSE_PASSWORD"],
    }
    text = write_blade(secret)
    ast.parse(text)
    assert read_blade(text) == secret | {"code": "", "template": None}


@pytest.mark.parametrize(
    "command",
    [
        "SELECT 'it''s' AS x",  # quotes of every kind
        "SELECT \"a\" FROM t WHERE b LIKE 'x\\_%' ESCAPE '\\'",  # backslashes
        "SELECT 1 -- '''triple'''\n",  # the literal string's own delimiter
        'catalog = [{"a": """b"""}]',
        "trailing spaces   \n\n",
    ],
)
def test_any_catalog_command_comes_back_exactly(command: str) -> None:
    blade = SQL_BLADE | {"catalog": {"language": "sql", "command": command}}
    assert read_blade(write_blade(blade))["catalog"]["command"] == command


def test_no_secret_value_is_ever_written() -> None:
    with pytest.raises(PortableError, match="never"):
        write_blade(SQL_BLADE | {"secrets": {"password": "hunter22"}})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("import os\n", "no swissarmygrid-blade header"),
        ("# /// swissarmygrid-blade\n# format = 1\n# name = 'x'\n", "never closed"),
        (
            "# /// swissarmygrid-blade\n# format = 9\n# name = 'x'\n# kind = 'other'\n# ///\nx = 1\n",
            "format 9",
        ),
        ("# /// swissarmygrid-blade\n# format = 1\n# name = = 'x'\n# ///\nx = 1\n", "header is not valid"),
        ("# /// swissarmygrid-blade\n# format = 1\n# kind = 'other'\n# ///\nx = 1\n", "name"),
        (
            "# /// swissarmygrid-blade\n# format = 1\n# name = 'x'\n# kind = 'other'\n# ///\nx = (\n",
            "not valid Python",
        ),
    ],
)
def test_broken_files_say_what_is_wrong(text: str, message: str) -> None:
    with pytest.raises(PortableError, match=message):
        read_blade(text)


def test_several_blades_from_a_zip_or_files() -> None:
    import io
    import zipfile

    vault = {
        "name": "Pantry",
        "kind": "vault",
        "variable": "get",
        "code": "def get(n):\n    return n\n",
        "vault_mapping": [{"env": "K", "secret": "k"}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("warehouse.blade.py", write_blade(SQL_BLADE))
        archive.writestr("pantry.blade.py", write_blade(vault))
        archive.writestr("README.txt", "not a blade")
        archive.writestr("../escape.blade.py", write_blade(SQL_BLADE | {"name": "Escape"}))
    found = read_blades([("pack.zip", buffer.getvalue())])
    assert [(f.file, f.blade["name"] if f.blade else None, f.error) for f in found] == [
        ("warehouse.blade.py", "Warehouse", None),
        ("pantry.blade.py", "Pantry", None),
        ("../escape.blade.py", "Escape", None),  # names inside a zip are only labels: nothing is extracted
    ]
    broken = read_blades([("bad.blade.py", b"import os\n"), ("binary.blade.py", b"\xff\xfe\x00")])
    assert [f.blade for f in broken] == [None, None] and all(f.error for f in broken)


def test_only_a_header_at_the_top_makes_a_file_a_blade() -> None:
    from swissarmygrid.domain.blades import is_blade

    blade = write_blade(SQL_BLADE)
    assert is_blade(blade)
    assert is_blade("#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n# Copyright Bakery Ltd\n\n" + blade)
    # A script that mentions the marker after its code, or in a string, is just a script.
    assert not is_blade("import os\n" + blade)
    assert not is_blade('"""Docs: # /// swissarmygrid-blade"""\n')
    assert not is_blade("x = 1\n")
    with pytest.raises(PortableError, match="no swissarmygrid-blade header"):
        read_blade("import os\n" + blade)


def test_description_and_tags_travel_in_the_header() -> None:
    blade = SQL_BLADE | {
        "description": "Sales warehouse, read only.\nAsk Ops for access.",
        "tags": ["sales", "orders-function-app"],
    }
    text = write_blade(blade)
    assert '# tags = ["sales", "orders-function-app"]' in text
    back = read_blade(text)
    assert back["description"] == blade["description"] and back["tags"] == blade["tags"]
