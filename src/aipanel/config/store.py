"""settings.json: load, validate, save atomically, and audit every change.

The server and the CLI (`aipanel settings set`, run beside it) may both write, so each change
is a read-modify-write under a lock file, and the server picks up the other's writes by the
file's modification time.
"""

import contextlib
import fcntl
import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aipanel import yamlio
from aipanel.audit import Audit
from aipanel.config.settings import Settings, validate
from aipanel.config.site import SiteConfig
from aipanel.files import atomic_write
from aipanel.files import writable as dir_writable


class SettingsError(ValueError):
    """A change that does not validate; nothing was saved."""


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out |= flatten(value, name + ".")
        else:
            out[name] = value
    return out


def merge(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """`changes` merged into `base`; keys may be nested mappings or dotted paths."""
    out = dict(base)
    for key, value in changes.items():
        head, _, rest = key.partition(".")
        if rest:
            value = {rest: value}
        if isinstance(value, Mapping) and isinstance(out.get(head), Mapping):
            out[head] = merge(out[head], value)
        else:
            out[head] = value
    return out


def describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}" for e in error.errors()
    )


class SettingsStore:
    def __init__(self, path: Path, audit: Audit, site: Callable[[], SiteConfig | None]) -> None:
        self.path = path
        self.audit = audit
        self.site = site
        self.error: str | None = None
        self._settings = Settings()
        self._stamp: tuple[int, int] | None = None
        self.load()

    @property
    def writable(self) -> bool:
        return dir_writable(self.path.parent)

    def get(self) -> Settings:
        return self._settings

    def conflict(self) -> str | None:
        """Why the saved settings disagree with the current site config, if they do."""
        try:
            validate(self._settings.model_dump(), self.site())
        except ValidationError as e:
            return describe(e)
        return None

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    def load(self) -> None:
        """Read the file. A missing file is the defaults; an invalid one keeps what we had."""
        self._stamp = self._stat()
        if self._stamp is None:
            self._settings, self.error = Settings(), None
            return
        try:
            # Structure only: a conflict with the site config is reported, not fatal.
            self._settings = validate(json.loads(self.path.read_text()), None)
            self.error = None
        except (OSError, ValueError) as e:
            detail = describe(e) if isinstance(e, ValidationError) else str(e)
            self.error = f"{self.path} is not valid, using the last good settings: {detail}"

    def reload_if_changed(self) -> bool:
        if self._stat() == self._stamp:
            return False
        self.load()
        return True

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if not self.writable:
            raise SettingsError(f"the state directory {self.path.parent} is not writable")
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def update(self, changes: Mapping[str, Any], actor: str) -> Settings:
        """Apply changes (nested, or dotted keys), validated against the site config."""
        with self._locked():
            self.load()
            return self._write(merge(self._settings.model_dump(mode="json"), changes), actor)

    def replace(self, data: Mapping[str, Any], actor: str) -> Settings:
        """Replace every setting (an import); keys not given return to their defaults."""
        with self._locked():
            self.load()
            return self._write(dict(data), actor, event="settings.imported")

    def export_yaml(self) -> str:
        return yamlio.dump(self._settings.model_dump(mode="json"))

    def _write(self, data: dict[str, Any], actor: str, event: str = "settings.changed") -> Settings:
        try:
            new = validate(data, self.site())
        except ValidationError as e:
            raise SettingsError(describe(e)) from None
        before = flatten(self._settings.model_dump(mode="json"))
        after = flatten(new.model_dump(mode="json"))
        changes = {
            key: {"from": before.get(key), "to": after.get(key)}
            for key in sorted(before.keys() | after.keys())
            if before.get(key) != after.get(key)
        }
        if not changes and self._stamp is not None:
            return self._settings
        atomic_write(self.path, json.dumps(new.model_dump(mode="json"), indent=2) + "\n")
        self._settings, self.error, self._stamp = new, None, self._stat()
        self.audit.record(event, actor, changes=changes)
        return new
