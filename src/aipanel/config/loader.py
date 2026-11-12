"""Loading the site config, and reloading it while the panel runs.

The file is mounted from a ConfigMap, so it changes under a running panel. It is re-read when
it changes and every 30 seconds regardless; a version that does not validate is not loaded:
the panel keeps the last good one and says so (and `aipanel_config_valid` drops to 0).
"""

import logging
import time
from pathlib import Path

from pydantic import ValidationError

from aipanel import yamlio
from aipanel.config.site import SiteConfig

log = logging.getLogger("aipanel.config")

FORCED_RELOAD_SECONDS = 30


class ConfigError(ValueError):
    pass


def parse(text: str, demo: bool) -> SiteConfig:
    try:
        data = yamlio.load(text)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    try:
        config = SiteConfig.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}"
            for err in e.errors()
        )
        raise ConfigError(problems) from None
    if config.auth.mode == "none" and not demo:
        raise ConfigError("auth.mode none is accepted only in DEMO mode (AIPANEL_DEMO=1)")
    return config


def load_file(path: Path, demo: bool) -> SiteConfig:
    try:
        text = path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    return parse(text, demo)


class SiteConfigLoader:
    """Holds the last good site config. The first load must succeed: no config, no start."""

    def __init__(self, path: Path | None, demo: bool, initial: SiteConfig | None = None) -> None:
        self.path = path
        self.demo = demo
        self.error: str | None = None
        self._stamp: tuple[int, int] | None = None
        self._loaded_at = time.monotonic()
        if path is not None:
            self._stamp = self._stat()
            initial = load_file(path, demo)
        if initial is None:
            raise ConfigError("no site config given")
        self.current = initial

    @property
    def valid(self) -> bool:
        return self.error is None

    def _stat(self) -> tuple[int, int] | None:
        assert self.path is not None
        try:
            st = self.path.stat()  # follows the ConfigMap's symlinks
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def reload_if_changed(self) -> bool:
        """Re-read on change, or when the forced interval passed. True when a new config loaded."""
        if self.path is None:
            return False
        stamp = self._stat()
        due = time.monotonic() - self._loaded_at >= FORCED_RELOAD_SECONDS
        if stamp == self._stamp and not due:
            return False
        self._stamp, self._loaded_at = stamp, time.monotonic()
        try:
            config = load_file(self.path, self.demo)
        except ConfigError as e:
            if self.error != str(e):
                log.error("site config not loaded, keeping the last good one: %s", e)
            self.error = str(e)
            return False
        if self.error is not None:
            log.info("site config valid again")
        self.error = None
        if config == self.current:
            return False
        self.current = config
        log.info("site config reloaded")
        return True
