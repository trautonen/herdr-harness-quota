from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import PROVIDERS
from .model import complete_windows
from .service import refresh_provider
from .storage import read_snapshot


def error_details(error: Exception) -> dict[str, str]:
    message = str(error)
    lowered = message.lower()
    if "credential" in lowered:
        code = "credentials_not_found"
    elif "http 401" in lowered or "http 403" in lowered:
        code = "authentication_failed"
    elif "timed out" in lowered or "timeout" in lowered:
        code = "timeout"
    elif "valid json" in lowered or "json object" in lowered or "quota windows" in lowered:
        code = "invalid_response"
    else:
        code = "refresh_failed"
    return {"code": code, "message": message}


def build_report(
    provider: str,
    refresh: str,
    stale_after: int,
    timeout: float,
) -> dict[str, Any]:
    providers = PROVIDERS if provider == "all" else (provider,)
    snapshots = {current: read_snapshot(current, max_age=None) for current in providers}
    now = int(time.time())
    refresh_errors: dict[str, dict[str, str]] = {}
    to_refresh = []
    for current in providers:
        snapshot = snapshots[current]
        is_stale = snapshot is None or now - int(snapshot["capturedAt"]) > stale_after
        if refresh == "always" or (refresh == "stale" and is_stale):
            to_refresh.append(current)

    if to_refresh:
        with ThreadPoolExecutor(max_workers=len(to_refresh)) as executor:
            futures = {
                executor.submit(refresh_provider, current, timeout): current
                for current in to_refresh
            }
            for future in as_completed(futures):
                current = futures[future]
                try:
                    future.result()
                    snapshots[current] = read_snapshot(current, max_age=None)
                except (RuntimeError, OSError) as error:
                    refresh_errors[current] = error_details(error)

    generated_at = int(time.time())
    report_providers = []
    for current in providers:
        snapshot = snapshots[current]
        error = refresh_errors.get(current)
        if snapshot is None:
            if error is None:
                error = {"code": "cache_missing", "message": "No cached quota is available"}
            status = "unavailable"
            captured_at = None
            windows = []
        else:
            captured_at = int(snapshot["capturedAt"])
            windows = complete_windows(snapshot["windows"])
            if not windows:
                status = "unavailable"
                captured_at = None
                error = error or {
                    "code": "invalid_response",
                    "message": "Cached quota has no usable windows",
                }
            elif error is not None or generated_at - captured_at > stale_after:
                status = "stale"
            else:
                status = "ok"
        report_providers.append(
            {
                "provider": current,
                "status": status,
                "capturedAt": captured_at,
                "windows": windows,
                "error": error,
            }
        )

    return {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "providers": report_providers,
    }
