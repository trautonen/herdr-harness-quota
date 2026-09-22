from __future__ import annotations

import os
import subprocess
import sys
import time

from .providers import claude, codex, cursor
from .storage import cache_directory

REFRESHERS = {
    "claude": claude.refresh,
    "codex": codex.refresh,
    "cursor": cursor.refresh,
}


def refresh_provider(provider: str, timeout: float = 12.0) -> None:
    REFRESHERS[provider](timeout)


def refresh_in_background(provider: str) -> None:
    lock_path = cache_directory() / f".{provider}-refresh.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            if time.time() - lock_path.stat().st_mtime < 30:
                return
            lock_path.unlink()
        except OSError:
            return
        return refresh_in_background(provider)
    os.close(descriptor)
    subprocess.Popen(
        [sys.executable, "-m", "harness_quota", "_background-refresh", provider],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
