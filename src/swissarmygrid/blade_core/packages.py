"""The pip packages a blade needs, and which this Python is missing.

A blade's imports are read from its code (code.py). Each top-level module maps to the package that
installs it, where the two names differ (`snowflake.connector` comes from
`snowflake-connector-python`, `yaml` from `PyYAML`); a few calls need a package the code never
imports (`pd.read_excel` needs openpyxl). Standard library modules need nothing. A module this table
does not know is assumed to install under its own name, and is marked as a guess.
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
from dataclasses import dataclass

from swissarmygrid.blade_core.code import read

# Dotted module (the longest that matches wins) -> pip package.
PIP_NAMES: dict[str, str] = {
    "pyodbc": "pyodbc",
    "pymssql": "pymssql",
    "mssql_python": "mssql-python",
    "psycopg": "psycopg[binary]",
    "psycopg2": "psycopg2-binary",
    "pg8000": "pg8000",
    "pymysql": "PyMySQL",
    "MySQLdb": "mysqlclient",
    "mysql.connector": "mysql-connector-python",
    "mariadb": "mariadb",
    "oracledb": "oracledb",
    "cx_Oracle": "cx_Oracle",
    "snowflake.connector": "snowflake-connector-python",
    "google.cloud.bigquery": "google-cloud-bigquery",
    "google.cloud.storage": "google-cloud-storage",
    "redshift_connector": "redshift-connector",
    "databricks.sql": "databricks-sql-connector",
    "ibm_db": "ibm_db",
    "ibm_db_dbi": "ibm_db",
    "teradatasql": "teradatasql",
    "trino": "trino",
    "prestodb": "presto-python-client",
    "clickhouse_connect": "clickhouse-connect",
    "hdbcli": "hdbcli",
    "vertica_python": "vertica-python",
    "sqlalchemy": "SQLAlchemy",
    "pandas": "pandas",
    "polars": "polars",
    "pyarrow": "pyarrow",
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd",
    "pyxlsb": "pyxlsb",
    "duckdb": "duckdb",
    "requests": "requests",
    "httpx": "httpx",
    "boto3": "boto3",
    "s3fs": "s3fs",
    "azure.storage.blob": "azure-storage-blob",
    "azure.identity": "azure-identity",
    "azure.keyvault.secrets": "azure-keyvault-secrets",
    "google.cloud.secretmanager": "google-cloud-secret-manager",
    "hvac": "hvac",
    "onepassword": "onepassword-sdk",
    "keyring": "keyring",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "pymongo": "pymongo",
    "redis": "redis",
    "elasticsearch": "elasticsearch",
    "simple_salesforce": "simple-salesforce",
    "gspread": "gspread",
}

# Calls that need a package the code may never import: `pd.read_excel(...)` reads .xlsx with openpyxl.
CALL_NEEDS: dict[str, str] = {
    "read_excel": "openpyxl",
    "to_excel": "openpyxl",
    "read_parquet": "pyarrow",
    "to_parquet": "pyarrow",
    "read_xml": "lxml",
    "read_html": "lxml",
}

# Never installed by pip: the standard library, and SwissArmyGrid itself (sag.blade gives it).
_PROVIDED: frozenset[str] = frozenset({*sys.stdlib_module_names, "swissarmygrid", "__future__"})


@dataclass(frozen=True)
class Package:
    module: str  # the top-level module Python imports
    pip: str  # what installs it
    guessed: bool  # not in PIP_NAMES: assumed to install under its own name
    installed: bool  # importable in this Python

    def to_json(self) -> dict[str, object]:
        return {"module": self.module, "pip": self.pip, "guessed": self.guessed, "installed": self.installed}


def pip_name(dotted: str) -> tuple[str, bool]:
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        known = PIP_NAMES.get(".".join(parts[:end]))
        if known:
            return known, False
    return parts[0], True


def _installed(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError):
        return False


def needed(code: str) -> list[Package]:
    """Every package the blade needs, whether installed here or not, in the order the code uses them."""
    found = read(code)
    packages: dict[str, Package] = {}
    for dotted in found.imports:
        if dotted.split(".")[0] in _PROVIDED:
            continue
        pip, guessed = pip_name(dotted)
        if pip not in packages:
            packages[pip] = Package(dotted, pip, guessed, _installed(dotted))
    for call in found.calls:
        extra = CALL_NEEDS.get(call)
        if extra and extra not in packages:
            packages[extra] = Package(extra, extra, False, _installed(extra))
    return list(packages.values())


def install_command(pips: list[str], python: str | None = None) -> str:
    """`uv pip install ...` for these packages; into `python`'s environment when given."""
    target = f"--python {shlex.quote(python)} " if python else ""
    return f"uv pip install {target}" + " ".join(shlex.quote(p) for p in pips)


def missing_module_hint(module: str) -> str:
    """The sentence for an ImportError: what to install, and the command that does it."""
    pip, _ = pip_name(module)
    return (
        f"This blade needs {pip}, which is not installed in this Python. "
        f"Install it with: {install_command([pip])}"
    )
