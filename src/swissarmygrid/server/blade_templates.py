"""Starter blades: the most used databases, common file patterns and vaults (plan, the owner's request).

Each template is a starting point the user edits on the save screen: the driver's import, the
connection with placeholders (`my-server`, `my_database`), `secret("password")` where a password goes,
the variable it makes, and a catalog command that lists the source's tables and columns. Database
catalogs are SQL in the source's own dialect; file catalogs are Python that reads only headers and a
few rows; vault templates make `get`, a function from a secret's name to its value.

The packages each needs are read from the code itself (packages.py), so the save screen can offer
to install them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    group: str  # "Databases", "Files", "Vaults", "Other"
    kind: str  # "sql", "other" or "vault"
    code: str
    variable: str
    dialect: str = "other"
    catalog_language: str | None = None  # "sql" or "python"
    catalog_command: str | None = None
    vault_mapping: tuple[tuple[str, str], ...] = ()  # (environment variable, secret name) examples
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "dialect": self.dialect,
            "code": self.code,
            "variable": self.variable,
            "catalog": (
                {"language": self.catalog_language, "command": self.catalog_command}
                if self.catalog_language and self.catalog_command
                else None
            ),
            "vault_mapping": [{"env": e, "secret": s} for e, s in self.vault_mapping],
            "note": self.note,
        }


# ===== catalog queries =====

INFORMATION_SCHEMA = """SELECT c.table_catalog, c.table_schema, c.table_name, t.table_type,
       c.column_name, c.data_type, c.is_nullable, c.ordinal_position
FROM information_schema.columns AS c
JOIN information_schema.tables AS t
  ON t.table_catalog = c.table_catalog
 AND t.table_schema = c.table_schema
 AND t.table_name = c.table_name
WHERE c.table_schema NOT IN ('information_schema', 'INFORMATION_SCHEMA', 'pg_catalog', 'sys')
ORDER BY c.table_schema, c.table_name, c.ordinal_position"""

SQLITE = """SELECT 'main' AS table_schema, m.name AS table_name, m.type AS table_type,
       p.name AS column_name, p.type AS data_type,
       CASE WHEN p."notnull" = 1 THEN 'NO' ELSE 'YES' END AS is_nullable,
       p.cid + 1 AS ordinal_position
FROM sqlite_master AS m
JOIN pragma_table_info(m.name) AS p
WHERE m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite_%'
ORDER BY m.name, p.cid"""

ORACLE = """SELECT c.owner, c.table_name, o.object_type AS table_type, c.column_name, c.data_type,
       c.nullable, c.column_id
FROM all_tab_columns c
JOIN all_objects o ON o.owner = c.owner AND o.object_name = c.table_name
                  AND o.object_type IN ('TABLE', 'VIEW')
WHERE c.owner NOT IN ('SYS', 'SYSTEM', 'XDB', 'MDSYS', 'CTXSYS', 'ORDSYS', 'OUTLN', 'DBSNMP',
                      'APEX_PUBLIC_USER')
ORDER BY c.owner, c.table_name, c.column_id"""

DB2 = """SELECT c.tabschema AS table_schema, c.tabname AS table_name,
       CASE t.type WHEN 'V' THEN 'VIEW' ELSE 'TABLE' END AS table_type,
       c.colname AS column_name, c.typename AS data_type,
       CASE c.nulls WHEN 'Y' THEN 'YES' ELSE 'NO' END AS is_nullable, c.colno + 1 AS ordinal_position
FROM syscat.columns c
JOIN syscat.tables t ON t.tabschema = c.tabschema AND t.tabname = c.tabname
WHERE c.tabschema NOT LIKE 'SYS%'
ORDER BY c.tabschema, c.tabname, c.colno"""

TERADATA = """SELECT c.DatabaseName AS table_schema, c.TableName AS table_name,
       CASE t.TableKind WHEN 'V' THEN 'VIEW' ELSE 'TABLE' END AS table_type,
       c.ColumnName AS column_name, c.ColumnType AS data_type, c.Nullable AS is_nullable,
       c.ColumnId AS ordinal_position
FROM DBC.ColumnsV c
JOIN DBC.TablesV t ON t.DatabaseName = c.DatabaseName AND t.TableName = c.TableName
WHERE c.DatabaseName NOT IN ('DBC', 'SYSLIB', 'SystemFe', 'SYSUDTLIB', 'TD_SYSFNLIB')
ORDER BY 1, 2, 7"""

SAP_HANA = """SELECT schema_name AS table_schema, table_name, 'TABLE' AS table_type, column_name,
       data_type_name AS data_type, is_nullable, position AS ordinal_position
FROM sys.table_columns
WHERE schema_name NOT LIKE '_SYS%' AND schema_name NOT IN ('SYS', 'SYSTEM')
UNION ALL
SELECT schema_name, view_name, 'VIEW', column_name, data_type_name, is_nullable, position
FROM sys.view_columns
WHERE schema_name NOT LIKE '_SYS%' AND schema_name NOT IN ('SYS', 'SYSTEM')
ORDER BY 1, 2, 7"""

VERTICA = """SELECT table_schema, table_name, 'TABLE' AS table_type, column_name, data_type,
       CASE WHEN is_nullable THEN 'YES' ELSE 'NO' END AS is_nullable, ordinal_position
FROM v_catalog.columns
UNION ALL
SELECT table_schema, table_name, 'VIEW', column_name, data_type, 'YES', ordinal_position
FROM v_catalog.view_columns
ORDER BY 1, 2, 7"""

BIGQUERY = """SELECT table_catalog, table_schema, table_name, column_name, data_type, is_nullable,
       ordinal_position
FROM `region-us`.INFORMATION_SCHEMA.COLUMNS
ORDER BY table_schema, table_name, ordinal_position"""

# Any ODBC source: pyodbc's own catalog call, which every ODBC driver answers.
ODBC_CATALOG = """[
    {
        "table_catalog": c.table_cat,
        "table_schema": c.table_schem,
        "table_name": c.table_name,
        "column_name": c.column_name,
        "data_type": c.type_name,
        "is_nullable": c.is_nullable,
        "ordinal_position": c.ordinal_position,
    }
    for c in conn.cursor().columns()
]"""

CLICKHOUSE_CATALOG = """conn.query_df(
    "SELECT database AS table_schema, table AS table_name, name AS column_name, type AS data_type, "
    "position AS ordinal_position FROM system.columns "
    "WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema') "
    "ORDER BY database, table, position"
)"""


def _db(
    id: str,
    label: str,
    dialect: str,
    code: str,
    catalog: str,
    note: str = "",
    language: str = "sql",
) -> Template:
    return Template(id, label, "Databases", "sql", code, "conn", dialect, language, catalog, note=note)


DATABASES: tuple[Template, ...] = (
    _db(
        "sqlserver",
        "SQL Server",
        "sqlserver",
        """import pyodbc

conn = pyodbc.connect(
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=my-server;"
    "Database=my_database;"
    "Trusted_Connection=yes;"  # Windows sign-in; or UID=...;PWD= + secret("password")
    "Encrypt=yes;TrustServerCertificate=yes;"
)""",
        INFORMATION_SCHEMA,
        "Needs Microsoft's ODBC Driver 18 for SQL Server on this computer.",
    ),
    _db(
        "azuresql",
        "Azure SQL Database / Synapse",
        "sqlserver",
        """import pyodbc

conn = pyodbc.connect(
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=tcp:my-server.database.windows.net,1433;"
    "Database=my_database;"
    "Authentication=ActiveDirectoryInteractive;"  # signs in with your Microsoft account
    "Encrypt=yes;"
)""",
        INFORMATION_SCHEMA,
        "Needs Microsoft's ODBC Driver 18 for SQL Server on this computer.",
    ),
    _db(
        "postgres",
        "PostgreSQL",
        "postgres",
        """import psycopg

conn = psycopg.connect(
    host="my-server",
    port=5432,
    dbname="my_database",
    user="my_user",
    password=secret("password"),
)""",
        INFORMATION_SCHEMA,
    ),
    _db(
        "mysql",
        "MySQL",
        "mysql",
        """import pymysql

conn = pymysql.connect(
    host="my-server",
    port=3306,
    database="my_database",
    user="my_user",
    password=secret("password"),
)""",
        INFORMATION_SCHEMA.replace("'pg_catalog', 'sys'", "'mysql', 'performance_schema', 'sys'"),
    ),
    _db(
        "mariadb",
        "MariaDB",
        "mysql",
        """import mariadb

conn = mariadb.connect(
    host="my-server",
    port=3306,
    database="my_database",
    user="my_user",
    password=secret("password"),
)""",
        INFORMATION_SCHEMA.replace("'pg_catalog', 'sys'", "'mysql', 'performance_schema', 'sys'"),
    ),
    _db(
        "oracle",
        "Oracle",
        "oracle",
        """import oracledb

conn = oracledb.connect(
    user="my_user",
    password=secret("password"),
    dsn="my-server:1521/my_service",
)""",
        ORACLE,
        "oracledb's thin mode needs no Oracle client install.",
    ),
    _db(
        "sqlite",
        "SQLite",
        "sqlite",
        """import sqlite3

conn = sqlite3.connect(r"C:\\data\\my_database.sqlite")""",
        SQLITE,
    ),
    _db(
        "duckdb",
        "DuckDB file",
        "duckdb",
        """import duckdb

conn = duckdb.connect(r"C:\\data\\my_database.duckdb", read_only=True)""",
        INFORMATION_SCHEMA,
    ),
    _db(
        "snowflake",
        "Snowflake",
        "snowflake",
        """import snowflake.connector

conn = snowflake.connector.connect(
    account="my_account",
    user="my_user",
    authenticator="externalbrowser",  # single sign-on; or password=secret("password")
    warehouse="my_warehouse",
    database="MY_DATABASE",
)""",
        INFORMATION_SCHEMA,
    ),
    _db(
        "bigquery",
        "Google BigQuery",
        "other",
        """from google.cloud import bigquery
from google.cloud.bigquery import dbapi

conn = dbapi.connect(bigquery.Client(project="my-project"))""",
        BIGQUERY,
        "Signs in with `gcloud auth application-default login`. Change region-us to your region.",
    ),
    _db(
        "redshift",
        "Amazon Redshift",
        "postgres",
        """import redshift_connector

conn = redshift_connector.connect(
    host="my-cluster.abc123.us-east-1.redshift.amazonaws.com",
    database="my_database",
    user="my_user",
    password=secret("password"),
)""",
        INFORMATION_SCHEMA,
    ),
    _db(
        "databricks",
        "Databricks",
        "other",
        """from databricks import sql

conn = sql.connect(
    server_hostname="adb-1234567890.12.azuredatabricks.net",
    http_path="/sql/1.0/warehouses/my_warehouse",
    access_token=secret("token"),
)""",
        INFORMATION_SCHEMA,
    ),
    _db(
        "db2",
        "IBM Db2",
        "other",
        """import ibm_db_dbi

conn = ibm_db_dbi.connect(
    "DATABASE=my_database;HOSTNAME=my-server;PORT=50000;PROTOCOL=TCPIP;"
    "UID=my_user;PWD=" + secret("password") + ";"
)""",
        DB2,
    ),
    _db(
        "teradata",
        "Teradata",
        "other",
        """import teradatasql

conn = teradatasql.connect(host="my-server", user="my_user", password=secret("password"))""",
        TERADATA,
    ),
    _db(
        "trino",
        "Trino / Presto",
        "trino",
        """import trino

conn = trino.dbapi.connect(
    host="my-server",
    port=443,
    user="my_user",
    catalog="hive",
    http_scheme="https",
)""",
        INFORMATION_SCHEMA,
    ),
    _db(
        "clickhouse",
        "ClickHouse",
        "other",
        """import clickhouse_connect

conn = clickhouse_connect.get_client(
    host="my-server",
    username="my_user",
    password=secret("password"),
)""",
        CLICKHOUSE_CATALOG,
        language="python",
    ),
    _db(
        "saphana",
        "SAP HANA",
        "other",
        """from hdbcli import dbapi

conn = dbapi.connect(address="my-server", port=30015, user="my_user", password=secret("password"))""",
        SAP_HANA,
    ),
    _db(
        "vertica",
        "Vertica",
        "other",
        """import vertica_python

conn = vertica_python.connect(
    host="my-server",
    port=5433,
    database="my_database",
    user="my_user",
    password=secret("password"),
)""",
        VERTICA,
    ),
    _db(
        "sybase",
        "SAP ASE (Sybase)",
        "sqlserver",
        """import pyodbc

conn = pyodbc.connect(
    "Driver={Adaptive Server Enterprise};Server=my-server;Port=5000;"
    "Database=my_database;UID=my_user;PWD=" + secret("password") + ";"
)""",
        ODBC_CATALOG,
        "Needs SAP's ASE ODBC driver.",
        language="python",
    ),
    _db(
        "access",
        "Microsoft Access",
        "other",
        """import pyodbc

conn = pyodbc.connect(
    r"Driver={Microsoft Access Driver (*.mdb, *.accdb)};"
    r"DBQ=C:\\data\\my_database.accdb;"
)""",
        ODBC_CATALOG,
        "Needs the Microsoft Access Database Engine (Windows).",
        language="python",
    ),
    _db(
        "odbc",
        "Any ODBC data source",
        "other",
        """import pyodbc

conn = pyodbc.connect("DSN=my_data_source")""",
        ODBC_CATALOG,
        "Uses a data source set up in the ODBC Data Source Administrator.",
        language="python",
    ),
)

FILES: tuple[Template, ...] = (
    Template(
        "csv",
        "A CSV file",
        "Files",
        "other",
        """from pathlib import Path

path = Path(r"C:\\data\\orders.csv")""",
        "path",
        catalog_language="python",
        catalog_command="""import pandas as pd

sample = pd.read_csv(path, nrows=1000)
[
    {"table_schema": path.parent.name, "table_name": path.stem,
     "column_name": str(name), "data_type": str(kind)}
    for name, kind in sample.dtypes.items()
]""",
    ),
    Template(
        "excel",
        "An Excel workbook",
        "Files",
        "other",
        """from pathlib import Path

path = Path(r"C:\\data\\budget.xlsx")""",
        "path",
        catalog_language="python",
        catalog_command="""import pandas as pd

sheets = pd.read_excel(path, sheet_name=None, nrows=200)  # every sheet, its first rows only
[
    {"table_schema": path.stem, "table_name": sheet, "column_name": str(name), "data_type": str(kind)}
    for sheet, frame in sheets.items()
    for name, kind in frame.dtypes.items()
]""",
    ),
    Template(
        "parquet",
        "A Parquet file",
        "Files",
        "other",
        """from pathlib import Path

path = Path(r"C:\\data\\events.parquet")""",
        "path",
        catalog_language="python",
        catalog_command="""import pyarrow.parquet as pq

[
    {"table_schema": path.parent.name, "table_name": path.stem,
     "column_name": f.name, "data_type": str(f.type)}
    for f in pq.read_schema(path)
]""",
    ),
    Template(
        "json",
        "A JSON file",
        "Files",
        "other",
        """from pathlib import Path

path = Path(r"C:\\data\\records.json")""",
        "path",
        catalog_language="python",
        catalog_command="""import pandas as pd

sample = pd.read_json(path).head(1000)
[
    {"table_schema": path.parent.name, "table_name": path.stem,
     "column_name": str(name), "data_type": str(kind)}
    for name, kind in sample.dtypes.items()
]""",
    ),
    Template(
        "folder",
        "A folder of files",
        "Files",
        "other",
        """from pathlib import Path

folder = Path(r"C:\\data\\exports")""",
        "folder",
        catalog_language="python",
        catalog_command="""import pandas as pd
import pyarrow.parquet as pq


def columns_of(file):
    if file.suffix.lower() == ".parquet":
        return [(f.name, str(f.type)) for f in pq.read_schema(file)]
    if file.suffix.lower() in (".xlsx", ".xlsm"):
        return [(str(n), str(k)) for n, k in pd.read_excel(file, nrows=200).dtypes.items()]
    return [(str(n), str(k)) for n, k in pd.read_csv(file, nrows=1000).dtypes.items()]


[
    {"table_schema": folder.name, "table_name": file.name, "column_name": name, "data_type": kind}
    for file in sorted(folder.iterdir())
    if file.suffix.lower() in (".csv", ".txt", ".parquet", ".xlsx", ".xlsm")
    for name, kind in columns_of(file)
]""",
    ),
)


def _vault(id: str, label: str, code: str, note: str = "") -> Template:
    return Template(
        id,
        label,
        "Vaults",
        "vault",
        code,
        "get",
        vault_mapping=(("WAREHOUSE_PASSWORD", "warehouse-password"),),
        note=note,
    )


VAULTS: tuple[Template, ...] = (
    _vault(
        "azure_key_vault",
        "Azure Key Vault",
        """from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

client = SecretClient(vault_url="https://my-vault.vault.azure.net", credential=DefaultAzureCredential())


def get(name):
    return client.get_secret(name).value""",
        "Signs in as you (Azure CLI, VS Code or Windows sign-in).",
    ),
    _vault(
        "aws_secrets_manager",
        "AWS Secrets Manager",
        """import boto3

client = boto3.client("secretsmanager", region_name="us-east-1")


def get(name):
    return client.get_secret_value(SecretId=name)["SecretString"]""",
        "Uses your AWS profile (`aws configure` or `aws sso login`).",
    ),
    _vault(
        "aws_parameter_store",
        "AWS Systems Manager Parameter Store",
        """import boto3

client = boto3.client("ssm", region_name="us-east-1")


def get(name):
    return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]""",
    ),
    _vault(
        "google_secret_manager",
        "Google Secret Manager",
        """from google.cloud import secretmanager

client = secretmanager.SecretManagerServiceClient()
project = "my-project"


def get(name):
    path = f"projects/{project}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode()""",
        "Signs in with `gcloud auth application-default login`.",
    ),
    _vault(
        "hashicorp_vault",
        "HashiCorp Vault",
        """import os

import hvac

client = hvac.Client(url="https://vault.example.com:8200", token=os.environ.get("VAULT_TOKEN"))


def get(name):
    path, _, key = name.partition("#")  # "app/warehouse#password": a key inside a KV v2 secret
    data = client.secrets.kv.v2.read_secret_version(path=path)["data"]["data"]
    return data[key or "value"]""",
    ),
    _vault(
        "onepassword",
        "1Password",
        """import subprocess


def get(name):
    # name is a secret reference, such as "op://Private/Warehouse/password" (1Password's CLI, signed in).
    return subprocess.run(["op", "read", name], capture_output=True, text=True, check=True).stdout.strip()""",
        "Needs the 1Password CLI (`op`), signed in.",
    ),
    _vault(
        "dotenv",
        "A .env file",
        """from dotenv import dotenv_values

values = dotenv_values(r"C:\\projects\\my_project\\.env")


def get(name):
    return values.get(name)""",
    ),
    _vault(
        "os_vault",
        "Windows Credential Manager / macOS Keychain",
        """import keyring

service = "my-service"


def get(name):
    return keyring.get_password(service, name)""",
    ),
)

OTHER: tuple[Template, ...] = (
    Template(
        "blank",
        "Blank (any code)",
        "Other",
        "other",
        """import requests

session = requests.Session()
session.headers["Authorization"] = "Bearer " + secret("token")""",
        "session",
    ),
)

TEMPLATES: tuple[Template, ...] = (*DATABASES, *FILES, *VAULTS, *OTHER)
BY_ID: dict[str, Template] = {t.id: t for t in TEMPLATES}
