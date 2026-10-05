"""Small OS-backed file mutex; process exit releases the lock automatically."""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path


class FileMutex:
    """Exclusive lock across threads and processes, without deleting lock files.

    A mutex is not reentrant. Keep it only around local transactions, not network
    calls. A zero timeout makes one non-blocking acquisition attempt.
    """

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self._gate = threading.Lock()
        self._handle = None
        self._owner = None

    def acquire(self) -> None:
        owner = threading.get_ident()
        if self._owner == owner:
            raise RuntimeError("FileMutex does not support nested acquisition")
        deadline = time.monotonic() + self.timeout
        if not self._gate.acquire(timeout=self.timeout):
            raise TimeoutError("文件锁获取超时")
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b", buffering=0)
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    self._owner = owner
                    return
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("文件锁获取超时") from exc
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except BaseException:
            if handle is not None:
                handle.close()
            self._gate.release()
            raise

    def release(self) -> None:
        if self._handle is None:
            return
        if self._owner != threading.get_ident():
            raise RuntimeError("Only the thread that acquired the mutex may release it")
        handle = self._handle
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            self._owner = None
            self._gate.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
