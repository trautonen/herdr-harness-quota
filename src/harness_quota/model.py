from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import CANONICAL_MINUTES


def percentage(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, round(float(value))))


def numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and abs(number) != float("inf") else None


def epoch_seconds(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return int(timestamp)
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def normalized_window(
    minutes: int,
    used_percent: Any,
    resets_at: Any = None,
    estimated: bool = False,
) -> dict[str, Any] | None:
    used = percentage(used_percent)
    if used is None or minutes <= 0:
        return None
    return {
        "minutes": minutes,
        "usedPercent": used,
        "resetsAt": epoch_seconds(resets_at),
        "estimated": estimated,
    }


def complete_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical: dict[int, dict[str, Any]] = {}
    for candidate in windows:
        minutes = candidate.get("minutes")
        if minutes not in CANONICAL_MINUTES:
            continue
        window = normalized_window(
            minutes,
            candidate.get("usedPercent"),
            candidate.get("resetsAt"),
            candidate.get("estimated") is True,
        )
        if window:
            canonical[minutes] = window
    if not canonical:
        return []

    reported = sorted(canonical.values(), key=lambda item: item["minutes"])
    if len(reported) == 1:
        estimated_used = reported[0]["usedPercent"]
    else:
        estimated_used = round(
            reported[0]["usedPercent"] * 0.30 + reported[-1]["usedPercent"] * 0.70
        )
    for minutes in CANONICAL_MINUTES:
        if minutes not in canonical:
            window = normalized_window(minutes, estimated_used, estimated=True)
            if window:
                canonical[minutes] = window

    return [canonical[minutes] for minutes in CANONICAL_MINUTES]
