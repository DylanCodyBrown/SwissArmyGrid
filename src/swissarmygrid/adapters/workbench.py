"""The Workbench's files: notebooks and scripts the user opens and saves (on their click only), and the
recent list (the last files opened, kept in the state folder). Nothing else is written."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_FILE_BYTES: int = 20 * 1024 * 1024
MAX_RECENT: int = 15


class Documents:
    def __init__(
        self, recent_file: Path, default_folder: Path, environments_file: Path | None = None
    ) -> None:
        self._recent = recent_file
        self.default_folder = default_folder
        # Each document's environment (document path -> its Python), as the user chose or made it.
        self._environments = environments_file or recent_file.with_name("environments.json")

    def read(self, path: str) -> tuple[str, str, float]:
        """The file's text, its full path and when it was last changed."""
        try:
            file = Path(path).expanduser().resolve()
            there = file.is_file()
        except OSError as error:
            raise FileNotFoundError(
                f"{path[:200]} is not a file this computer can open ({error.strerror})."
            ) from None
        if not there:
            raise FileNotFoundError(f"{file} is not there.")
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(
                f"{file.name} is over {MAX_FILE_BYTES // 1024 // 1024} MB: too large to open here."
            )
        return file.read_text(encoding="utf-8"), str(file), file.stat().st_mtime

    def modified(self, path: str) -> float | None:
        try:
            file = Path(path).expanduser()
            return file.stat().st_mtime if file.is_file() else None
        except OSError:
            return None

    def write(self, path: str, text: str) -> tuple[str, float]:
        """Write the file whole (a temporary file renamed over it, so a failed save leaves the old one).
        Its folder must exist, except the Workbench's own default folder, made on its first save."""
        try:
            file = Path(path).expanduser().resolve()
        except OSError as error:
            raise FileNotFoundError(
                f"{path[:200]} is not a place this computer can save ({error.strerror})."
            ) from None
        if file.parent == self.default_folder.resolve():
            file.parent.mkdir(parents=True, exist_ok=True)
        if not file.parent.is_dir():
            raise FileNotFoundError(f"The folder {file.parent} is not there.")
        handle, temporary = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".saving")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
                out.write(text)
            os.replace(temporary, file)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return str(file), file.stat().st_mtime

    def recent(self) -> list[dict[str, Any]]:
        try:
            listed = json.loads(self._recent.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for item in listed if isinstance(listed, list) else []:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                out.append(
                    {
                        "path": item["path"],
                        "opened": item.get("opened", 0),
                        "exists": Path(item["path"]).is_file(),
                    }
                )
        return out[:MAX_RECENT]

    def remember(self, path: str, when: float) -> None:
        kept = [r for r in self.recent() if r["path"] != path]
        listed = [{"path": path, "opened": when}, *({"path": r["path"], "opened": r["opened"]} for r in kept)]
        self._recent.parent.mkdir(parents=True, exist_ok=True)
        self._recent.write_text(json.dumps(listed[:MAX_RECENT], indent=1), encoding="utf-8")

    def _chosen(self) -> dict[str, str]:
        try:
            found = json.loads(self._environments.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}

    def environment(self, path: str) -> str | None:
        """The Python remembered for a document, when it is still there."""
        python = self._chosen().get(str(Path(path).expanduser().resolve()))
        return python if python and Path(python).is_file() else None

    def remember_environment(self, path: str, python: str) -> None:
        chosen = self._chosen()
        chosen[str(Path(path).expanduser().resolve())] = python
        self._environments.parent.mkdir(parents=True, exist_ok=True)
        self._environments.write_text(json.dumps(chosen, indent=1), encoding="utf-8")
