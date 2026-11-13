"""Export: what the user may ask for, and what an export says about where it came from.

The options of an export (format, delimiter, encoding, the formula guard), the formats there are,
and the provenance written beside the data. The writers that turn rows into CSV, Excel and Parquet
bytes are an adapter (adapters/export.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FORMULA_STARTS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


FORMATS: dict[str, tuple[str, str]] = {
    "csv": (".csv", "text/csv"),
    "txt": (".txt", "text/plain"),
    "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "parquet": (".parquet", "application/vnd.apache.parquet"),
}


class ExportError(ValueError):
    """The export cannot be made as asked. The message says why."""


@dataclass(frozen=True)
class ExportOptions:
    format: str = "csv"
    delimiter: str = ","
    header: bool = True
    quote_all: bool = False
    line_ending: str = "windows"  # or "unix"
    encoding: str = "utf-8"  # or "utf-8-sig" (with byte order mark, for Excel)
    blank: str = ""
    neutralize_formulas: bool = True
    sheet_name: str = "Data"
    provenance: bool = True
    compression: str = "snappy"  # parquet: snappy or zstd
    allow_cell_truncation: bool = False

    @staticmethod
    def from_query(params: dict[str, str]) -> ExportOptions:
        def flag(name: str, default: bool) -> bool:
            value = params.get(name)
            return default if value is None else value in ("1", "true", "yes")

        fmt = params.get("format", "csv")
        if fmt not in FORMATS:
            raise ExportError(f"format must be one of {', '.join(FORMATS)}.")
        delimiter = params.get("delimiter", "\t" if fmt == "txt" else ",")
        delimiter = {"tab": "\t", "pipe": "|", "comma": ",", "semicolon": ";"}.get(delimiter, delimiter)
        if len(delimiter) != 1 or delimiter in ('"', "\r", "\n"):
            raise ExportError("The delimiter must be one character other than a quote or line break.")
        line_ending = params.get("line_ending", "windows")
        encoding = params.get("encoding", "utf-8")
        compression = params.get("compression", "snappy")
        if line_ending not in ("windows", "unix"):
            raise ExportError("line_ending must be windows or unix.")
        if encoding not in ("utf-8", "utf-8-sig"):
            raise ExportError("encoding must be utf-8 or utf-8-sig.")
        if compression not in ("snappy", "zstd"):
            raise ExportError("compression must be snappy or zstd.")
        sheet = params.get("sheet_name", "Data")
        if not sheet or len(sheet) > 31 or any(ch in sheet for ch in "[]:*?/\\"):
            raise ExportError("Sheet names are 1-31 characters without [ ] : * ? / \\.")
        return ExportOptions(
            format=fmt,
            delimiter=delimiter,
            header=flag("header", True),
            quote_all=flag("quote_all", False),
            line_ending=line_ending,
            encoding=encoding,
            blank=params.get("blank", ""),
            neutralize_formulas=flag("neutralize_formulas", True),
            sheet_name=sheet,
            provenance=flag("provenance", True),
            compression=compression,
            allow_cell_truncation=flag("allow_cell_truncation", False),
        )


@dataclass(frozen=True)
class Provenance:
    source: str
    steps: list[str]
    settings: str
    exported_at: str
    rows: int
    extra: dict[str, Any] = field(default_factory=dict)

    def lines(self) -> list[tuple[str, str]]:
        out = [
            ("Source", self.source),
            ("Exported at", self.exported_at),
            ("Rows", f"{self.rows:,}"),
            ("Settings", self.settings),
            ("Steps", "none" if not self.steps else ""),
        ]
        out += [("", step) for step in self.steps]
        return out


def neutralize(text: str) -> str:
    return "'" + text if text.startswith(FORMULA_STARTS) else text
