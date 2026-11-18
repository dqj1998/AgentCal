"""One Chromium profile = one lock. Queue instead of racing."""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path


class ProfileBusy(TimeoutError):
    pass


class ProfileLease:
    def __init__(self, directory: str | Path, wait_s: float = 30.0, poll_s: float = 0.05):
        self.directory = Path(directory)
        self.wait_s = wait_s
        self.poll_s = poll_s
        self._fh = None

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / "profile.lock"
        handle = lock_path.open("a+")
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ProfileBusy(f"profile lock busy: {lock_path}")
                time.sleep(self.poll_s)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._fh = handle

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "ProfileLease":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
