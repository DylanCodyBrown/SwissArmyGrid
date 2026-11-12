"""Server settings and resource limits (plan section 6a).

One frozen dataclass so every limit lives in one place, the way a C# app keeps its options in
one IOptions<T> record. Defaults follow the plan. Nothing reads a settings file yet; tests and
the command line build a Settings with different values.

Sizes are in bytes and reported in MiB (1,048,576 bytes) so the unit is never ambiguous.
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

MIB: int = 1024 * 1024
GIB: int = 1024 * MIB

# The page's files: bundled inside the package when installed from a wheel (the plugin), or
# web/static in the repo when running from source (uv sync's editable install).
_PACKAGED_WEB: Path = Path(__file__).resolve().parent / "web_static"
_REPO_WEB: Path = Path(__file__).resolve().parents[2] / "web" / "static"


def physical_memory_bytes() -> int:
    """Total physical memory of this machine, read once at startup."""
    if sys.platform == "win32":

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return int(status.ullTotalPhys)
    return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))


def _state_dir() -> Path:
    override = os.environ.get("SWISSARMYGRID_STATE")
    return Path(override) if override else Path.home() / ".swissarmygrid"


def _default_dataset_cap() -> int:
    """1 GiB per dataset (room for Excel's 1,048,576 rows on a wide table), or
    SWISSARMYGRID_MAX_DATASET_MB when set; always within the total budget."""
    override = os.environ.get("SWISSARMYGRID_MAX_DATASET_MB", "")
    return int(override) * MIB if override.isdigit() and int(override) > 0 else GIB


EXCEL_MAX_ROWS: int = 1_048_576


def _default_total_budget() -> int:
    # 3 GiB for every dataset together, never above 50% of physical memory (plan section 6a).
    return min(3 * GIB, physical_memory_bytes() // 2)


@dataclass(frozen=True)
class Settings:
    """Limits the server enforces."""

    # Size of one dataset once loaded, measured as its Arrow buffer size.
    max_dataset_bytes: int = field(default_factory=_default_dataset_cap)
    # All datasets and pinned results together. Nothing is ever evicted to make room.
    max_total_bytes: int = field(default_factory=_default_total_budget)
    max_datasets: int = 50
    max_grids: int = 50  # open tabs at once, the same as max_datasets
    # Size of a push request body. JSON is several times larger than the same data in
    # memory, so this cap is separate from max_dataset_bytes; otherwise a dataset well under
    # the dataset cap would be refused just for arriving as JSON.
    max_push_body_bytes: int = 2 * GIB
    max_window_rows: int = 1000
    # Longer text is cut in the grid; the detail view and exports carry the full value.
    max_cell_chars: int = 500
    # Excel's sheet limit: the SQL box shows a result as large as a worksheet can hold.
    max_sql_rows: int = EXCEL_MAX_ROWS
    max_chart_points: int = 5_000
    max_response_bytes: int = 8 * MIB
    # The SQL box sends its whole result at once (grid windows are paged), so it gets room for it.
    max_sql_response_bytes: int = 1 * GIB
    # Warn in the status bar at this share of any limit.
    warn_fraction: float = 0.8
    # Seconds a SQL box or agent query may run before it is interrupted.
    query_timeout_seconds: float = 30.0
    # How long an agent's snapshot request waits for the open page to draw and upload its PNG, and the
    # largest picture the server accepts back (a 4K screen at 2x is a few megabytes).
    snapshot_timeout_seconds: float = 30.0
    max_snapshot_bytes: int = 16 * MIB
    # The Key Finder joins every column of two tables, so it gets longer before it is stopped.
    key_finder_timeout_seconds: float = 120.0
    # The Key Finder's search for the most distinct column combination reads the grid's first
    # rows only, so a large table answers in seconds.
    key_finder_unique_rows: int = 400_000
    # Worker threads running DuckDB queries. Grid windows jump ahead of profiling, SQL and
    # agent work in the queue (engine/workers.py).
    worker_threads: int = 4
    # The browser files: index.html, grid.css, and build/ from `npm run build`.
    web_root: Path = field(default=_PACKAGED_WEB if _PACKAGED_WEB.is_dir() else _REPO_WEB)
    # Where recipes and the audit log live (plan section 18, question 5: user folder for now).
    state_dir: Path = field(default_factory=_state_dir)
    # Exports requested by an agent (MCP) land only here, never as a browser download.
    export_dir: Path = field(default_factory=lambda: _state_dir() / "exports")

    @property
    def duckdb_memory_limit(self) -> str:
        """Working memory: twice the data budget (plan section 6a), never under 256 MiB so a
        small budget still leaves room for sorting. Spilling stays off."""
        return f"{max(256, (2 * self.max_total_bytes) // MIB)}MiB"
