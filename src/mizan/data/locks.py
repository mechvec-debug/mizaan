"""Single-flight locking so identical downloads never run twice:
 * KeyedLocks : in-process (threads of one run)
 * FileLock   : cross-process (two jobs on one machine / shared workspace); stale locks are broken after a timeout
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


class KeyedLocks:
    def __init__(self):
        self._guard, self._locks = threading.Lock(), {}

    def get(self, key: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())


class FileLockTimeout(TimeoutError):
    pass


@contextmanager
def file_lock(path: Path, *, timeout: float = 120.0, stale_after: float = 900.0, poll: float = 0.05,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
    path.parent.mkdir(parents=True, exist_ok=True)
    start, fd = clock(), None
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
        except FileExistsError:
            try:
                if clock() - path.stat().st_mtime > stale_after:     # crashed holder -> break the lock
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if clock() - start > timeout:
                raise FileLockTimeout(f"could not acquire {path} within {timeout}s")
            sleep(poll)
    try:
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)
