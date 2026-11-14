"""A notebook's own Python environment (the Workbench's New environment): the libraries offered, by
group and in presets, the import line each one gives, and what a request may ask for. Pure rules.

The environment is a virtual environment (a .venv folder, by default beside the notebook) with
ipykernel, so the Workbench can run code in it, and the libraries chosen. The Workbench remembers it
for the document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_PACKAGES: int = 60
PYTHON_VERSIONS: tuple[str, ...] = ("3.13", "3.12", "3.11", "3.10")
_PIP_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(\[[A-Za-z0-9,._-]{1,60}\])?([<>=!~]=?[A-Za-z0-9.*+!-]{1,40})?$"
)
_FOLDER_NAME = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")


@dataclass(frozen=True)
class Library:
    pip: str  # as pip knows it
    label: str  # what the dialog shows
    group: str
    imports: str = ""  # the line it adds to a notebook (empty: none, such as a driver used by a connection)
    note: str = ""


LIBRARIES: tuple[Library, ...] = (
    Library("pandas", "pandas", "Data", "import pandas as pd", "tables (DataFrames)"),
    Library("numpy", "NumPy", "Data", "import numpy as np", "arrays and maths"),
    Library("polars", "Polars", "Data", "import polars as pl", "fast DataFrames"),
    Library("pyarrow", "PyArrow", "Data", "import pyarrow as pa", "Arrow and Parquet"),
    Library("duckdb", "DuckDB", "Data", "import duckdb", "SQL over your DataFrames and files"),
    Library("matplotlib", "Matplotlib", "Charts", "import matplotlib.pyplot as plt"),
    Library("seaborn", "seaborn", "Charts", "import seaborn as sns", "statistical charts"),
    Library("plotly", "Plotly", "Charts", "import plotly.express as px", "interactive charts"),
    Library("altair", "Altair", "Charts", "import altair as alt", "Vega-Lite charts"),
    Library("scipy", "SciPy", "Statistics and models", "import scipy.stats as stats"),
    Library(
        "statsmodels",
        "statsmodels",
        "Statistics and models",
        "import statsmodels.api as sm",
        "regressions, time series",
    ),
    Library("scikit-learn", "scikit-learn", "Statistics and models", "import sklearn", "machine learning"),
    Library("openpyxl", "openpyxl", "Files", "", "reads and writes .xlsx"),
    Library("xlsxwriter", "XlsxWriter", "Files", "", "writes formatted .xlsx"),
    Library("sqlalchemy", "SQLAlchemy", "Databases", "import sqlalchemy as sa", "any database, one way"),
    Library("pyodbc", "pyodbc", "Databases", "", "SQL Server, Access, any ODBC"),
    Library("psycopg[binary]", "psycopg", "Databases", "", "PostgreSQL, Redshift"),
    Library("pymysql", "PyMySQL", "Databases", "", "MySQL, MariaDB"),
    Library("oracledb", "oracledb", "Databases", "", "Oracle"),
    Library("snowflake-connector-python", "Snowflake", "Databases", "", "Snowflake"),
    Library("requests", "Requests", "Web and APIs", "import requests", "calling web APIs"),
    Library("httpx", "HTTPX", "Web and APIs", "import httpx"),
    Library("tqdm", "tqdm", "Notebook helpers", "from tqdm.auto import tqdm", "progress bars"),
    Library("ipywidgets", "ipywidgets", "Notebook helpers", "", "widgets"),
)
PRESETS: dict[str, tuple[str, ...]] = {
    "Data analysis": ("pandas", "numpy", "pyarrow", "duckdb", "matplotlib", "openpyxl"),
    "Charts": ("pandas", "matplotlib", "seaborn", "plotly"),
    "Statistics and models": ("pandas", "numpy", "scipy", "statsmodels", "scikit-learn", "matplotlib"),
    "Databases": ("pandas", "sqlalchemy", "pyodbc", "psycopg[binary]"),
    "Just the basics": ("pandas",),
}
# Always installed: the kernel the Workbench runs code in.
ALWAYS: tuple[str, ...] = ("ipykernel",)


class EnvironmentRequestError(ValueError):
    """A new environment that cannot be made as asked (the message says why)."""


def catalog() -> dict[str, Any]:
    """What the New environment dialog offers."""
    return {
        "libraries": [
            {"pip": lib.pip, "label": lib.label, "group": lib.group, "imports": lib.imports, "note": lib.note}
            for lib in LIBRARIES
        ],
        "presets": {name: list(pips) for name, pips in PRESETS.items()},
        "versions": list(PYTHON_VERSIONS),
        "always": list(ALWAYS),
    }


def check_request(body: Any) -> tuple[str, str | None, list[str]]:
    """The folder to make, the Python version (None: the app's own), and the packages (the kernel's
    first), from what the dialog sent."""
    if not isinstance(body, dict):
        raise EnvironmentRequestError('Send {"folder": "...", "python": "3.12", "packages": ["pandas"]}.')
    folder = body.get("folder")
    if not isinstance(folder, str) or not folder.strip() or len(folder) > 1_000:
        raise EnvironmentRequestError("Say where the environment goes: a folder that does not exist yet.")
    if not is_full_path(folder.strip()):
        # A relative folder would land wherever the server was started (a sweep of odd arguments made
        # environments named "x" in the app's own folder).
        raise EnvironmentRequestError(
            "Give the environment's folder as a full path (C:\\Users\\…\\.venv or /home/…/.venv)."
        )
    version = body.get("python") or None
    if version is not None and version not in PYTHON_VERSIONS:
        raise EnvironmentRequestError(
            f"Pick a Python version: {', '.join(PYTHON_VERSIONS)}, or the app's own."
        )
    packages = body.get("packages") or []
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise EnvironmentRequestError(f"Send the packages as a list (at most {MAX_PACKAGES}).")
    wanted: list[str] = []
    for package in packages:
        if not isinstance(package, str) or not _PIP_NAME.match(package.strip()):
            raise EnvironmentRequestError(
                f"{package!r} is not a package name pip knows (such as pandas or psycopg[binary])."
            )
        wanted.append(package.strip())
    return folder.strip(), version, list(dict.fromkeys([*ALWAYS, *wanted]))


def is_full_path(path: str) -> bool:
    """A full path on Windows (C:\\…, \\\\server\\share) or elsewhere (/…), read from the text alone."""
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/)", path))


def folder_name_ok(name: str) -> bool:
    """A new environment's folder name: plain letters, digits, dots, dashes, spaces."""
    return bool(_FOLDER_NAME.match(name))


def import_lines(packages: list[str]) -> list[str]:
    """The import lines for the chosen libraries, in the dialog's order (none for drivers)."""
    chosen = set(packages)
    return [lib.imports for lib in LIBRARIES if lib.pip in chosen and lib.imports]
