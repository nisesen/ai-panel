"""Writing the state directory's files safely, and off the event loop.

The state directory may be on NFS, where a write can stall for seconds. Appends and saves go
to one background thread, in order, so a stalled mount slows only the files, never the page,
the poller or a running action. State files are 0640, so a backup job in the same group can
read them; the CSRF key is 0600.
"""

import contextlib
import logging
import os
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("aipanel.files")

MODE = 0o640
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aipanel-state")


def _logged(write: Callable[[], None]) -> None:
    try:
        write()
    except OSError as e:
        log.error("state not written: %s", e)


def in_background(write: Callable[[], None]) -> None:
    """Run a state write on the writer thread, after every write queued before it."""
    _writer.submit(_logged, write)


def flush(timeout: float | None = 30) -> None:
    """Wait until the writes queued so far are done (before reading a file back)."""
    _writer.submit(lambda: None).result(timeout)


def atomic_write(path: Path, text: str, mode: int = MODE) -> None:
    """Write a temporary file beside `path`, then rename it over: readers never see half a file.

    Safe on NFS with a single writer.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):  # not every file system can sync a directory
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def append_line(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def writable(directory: Path) -> bool:
    try:
        return directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)
    except OSError:
        return False
