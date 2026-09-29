from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .model import percentage
from .storage import cache_directory, read_private_file, write_private_file, write_snapshot

WEEK_MINUTES = 10080
MONTH_MINUTES = 43200
WEEK_SECONDS = WEEK_MINUTES * 60
MONTH_SECONDS = MONTH_MINUTES * 60
RETENTION_SECONDS = MONTH_SECONDS + WEEK_SECONDS
RESET_TOLERANCE_SECONDS = 24 * 60 * 60


def history_path(provider: str) -> Path:
    return cache_directory() / f"{provider}.history.jsonl"


def is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_history(provider: str) -> list[dict[str, Any]]:
    try:
        text = read_private_file(history_path(provider))
    except (OSError, UnicodeDecodeError):
        return []
    if text is None:
        return []
    samples = []
    for line in text.splitlines():
        try:
            sample = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(sample, dict)
            and is_integer(sample.get("capturedAt"))
            and isinstance(sample.get("windows"), list)
        ):
            samples.append(sample)
    return samples


def write_history(
    provider: str,
    samples: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    now: int,
) -> None:
    retained = [sample for sample in samples if sample["capturedAt"] >= now - RETENTION_SECONDS]
    retained.append({"capturedAt": now, "windows": windows})
    text = "".join(json.dumps(sample, separators=(",", ":")) + "\n" for sample in retained)
    write_private_file(history_path(provider), text)


def estimate_monthly_usage(
    samples: list[dict[str, Any]], weekly_window: dict[str, Any], now: int
) -> int | None:
    resets_at = weekly_window.get("resetsAt")
    if weekly_window.get("estimated") is True or not is_integer(resets_at):
        return None
    month_start = now - MONTH_SECONDS

    weekly_usage = []
    for sample in samples:
        for window in sample["windows"]:
            if not isinstance(window, dict) or window.get("minutes") != WEEK_MINUTES:
                continue
            if window.get("estimated") is True or not is_integer(window.get("resetsAt")):
                continue
            used = percentage(window.get("usedPercent"))
            if used is not None:
                weekly_usage.append((window["resetsAt"], used))

    # Reported reset times jitter by seconds, so samples of one week are grouped by closeness.
    periods: list[tuple[int, int]] = []
    for sample_resets_at, used in sorted(weekly_usage):
        if periods and sample_resets_at - periods[-1][0] <= RESET_TOLERANCE_SECONDS:
            periods[-1] = (sample_resets_at, max(used, periods[-1][1]))
        else:
            periods.append((sample_resets_at, used))
    peaks = {
        end: peak
        for end, peak in periods
        if abs(end - resets_at) > RESET_TOLERANCE_SECONDS and month_start < end <= now
    }
    if not peaks:
        return None

    known_usage = float(weekly_window["usedPercent"])
    known_span = float(min(max(now - (resets_at - WEEK_SECONDS), 0), WEEK_SECONDS))
    for end, peak in peaks.items():
        overlap = min(max((end - max(end - WEEK_SECONDS, month_start)) / WEEK_SECONDS, 0), 1)
        known_usage += peak * overlap
        known_span += overlap * WEEK_SECONDS
    return max(0, min(100, round(known_usage * WEEK_SECONDS / known_span)))


def record_snapshot(provider: str, windows: list[dict[str, Any]]) -> None:
    now = int(time.time())
    samples = read_history(provider)
    windows_by_minutes = {window["minutes"]: window for window in windows}
    monthly_window = windows_by_minutes.get(MONTH_MINUTES)
    weekly_window = windows_by_minutes.get(WEEK_MINUTES)
    if monthly_window and monthly_window["estimated"] and weekly_window:
        estimate = estimate_monthly_usage(samples, weekly_window, now)
        if estimate is not None:
            windows = [
                {**window, "usedPercent": estimate} if window is monthly_window else window
                for window in windows
            ]
    write_snapshot(provider, windows)
    write_history(provider, samples, windows, now)
