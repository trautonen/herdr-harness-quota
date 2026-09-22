from __future__ import annotations

from typing import Any

from .model import percentage

LABELS = {"claude": "cl", "codex": "cx", "cursor": "cr"}


def window_label(minutes: int) -> str:
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "1w"
    if minutes % 10080 == 0:
        return f"{minutes // 10080}w"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def format_chip(provider: str, snapshot: dict[str, Any] | None) -> str:
    if snapshot is None:
        return ""
    parts = []
    windows = sorted(snapshot["windows"], key=lambda item: int(item.get("minutes", 0)))
    for window in windows:
        minutes = window.get("minutes")
        used = percentage(window.get("usedPercent"))
        if not isinstance(minutes, int) or used is None:
            continue
        remaining = 100 - used
        warning = "!" if remaining < 20 else ""
        label = "" if provider == "cursor" and len(windows) == 1 else window_label(minutes)
        parts.append(f"{label}{remaining}%{warning}")
    return f"{LABELS[provider]} {' '.join(parts)}" if parts else ""
