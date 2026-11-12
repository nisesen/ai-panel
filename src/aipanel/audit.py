"""The audit log: one JSON line per event, appended to a file and printed to stdout."""

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("aipanel.audit")


class Audit:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: str, actor: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event}
        entry |= {"actor": actor, **fields}
        line = json.dumps(entry, default=str, separators=(",", ":"))
        log.info(line)
        if self.path is not None:
            try:
                with self._lock, self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:  # a read-only state directory must not stop an action
                log.error("audit log not written: %s", e)
        return entry

    def read(self, limit: int, query: str = "", actor: str = "") -> list[dict[str, Any]]:
        """The newest entries first, filtered by actor and by text anywhere in the entry."""
        if self.path is None or not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if query and query.lower() not in line.lower():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if actor and entry.get("actor") != actor:
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out
