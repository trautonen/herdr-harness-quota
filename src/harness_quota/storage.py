from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_MAX_AGE


def cache_directory() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = root / "herdr-harness-quota"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or path_stat.st_uid != os.getuid():
        raise RuntimeError(f"unsafe cache directory: {path}")
    path.chmod(0o700)
    return path


def snapshot_path(provider: str) -> Path:
    return cache_directory() / f"{provider}.json"


def write_snapshot(provider: str, windows: list[dict[str, Any]]) -> None:
    payload = {"provider": provider, "capturedAt": int(time.time()), "windows": windows}
    destination = snapshot_path(provider)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{provider}-", dir=destination.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, separators=(",", ":"))
            temporary_file.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def read_snapshot(provider: str, max_age: int | None = DEFAULT_MAX_AGE) -> dict[str, Any] | None:
    try:
        path = snapshot_path(provider)
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.getuid():
            return None
        with path.open(encoding="utf-8") as snapshot_file:
            snapshot = json.load(snapshot_file)
        captured_at = int(snapshot["capturedAt"])
        now = time.time()
        if captured_at > now + 60 or (max_age is not None and now - captured_at > max_age):
            return None
        if snapshot.get("provider") != provider or not isinstance(snapshot.get("windows"), list):
            return None
        return snapshot
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None
