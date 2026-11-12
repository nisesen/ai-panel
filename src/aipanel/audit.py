"""The audit log: one JSON line per event, appended to a file and printed to stdout.

Long values are cut, the file is rotated by size (one previous file is kept), and readers see
only its tail, so a flood of requests cannot make the log or the Activity page unbounded.
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aipanel import files

log = logging.getLogger("aipanel.audit")

MAX_TEXT = 300
MAX_BYTES = 5 * 1024 * 1024
TAIL_BYTES = 1024 * 1024


def short(value: Any) -> Any:
    """`value` with every long string cut."""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    if isinstance(value, dict):
        return {short(k): short(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [short(v) for v in value]
    return value


class Audit:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def record(self, event: str, actor: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event}
        entry |= short({"actor": actor, **fields})
        line = json.dumps(entry, default=str, separators=(",", ":"))
        log.info(line)
        if self.path is not None:
            files.in_background(lambda: self._append(line))
        return entry

    def _append(self, line: str) -> None:
        assert self.path is not None
        try:
            if self.path.stat().st_size > MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass
        files.append_line(self.path, line)

    def read(self, limit: int, query: str = "", actor: str = "") -> list[dict[str, Any]]:
        """The newest entries first, filtered by actor and by text anywhere in the entry."""
        if self.path is None:
            return []
        files.flush()
        try:
            with self.path.open("rb") as f:
                f.seek(max(0, f.seek(0, os.SEEK_END) - TAIL_BYTES))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if query and query.lower() not in line.lower():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # also the first, partial line of the tail
            if not isinstance(entry, dict) or (actor and entry.get("actor") != actor):
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out
