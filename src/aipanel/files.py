"""Writing the state directory's files safely."""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write a temporary file beside `path`, then rename it over: readers never see half a file.

    Safe on NFS with a single writer.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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


def writable(directory: Path) -> bool:
    return directory.is_dir() and os.access(directory, os.W_OK)
