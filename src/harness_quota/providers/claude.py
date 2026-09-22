from __future__ import annotations

import json
import os
import re
import shutil
from functools import cache
from pathlib import Path
from typing import Any

from ..auth import read_credentials, read_pi_credential, required_string
from ..model import complete_windows, normalized_window
from ..storage import write_snapshot
from ..transport import fetch_json

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
VERSION_PATTERN = re.compile(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b")


@cache
def user_agent() -> str:
    configured = os.environ.get("HARNESS_QUOTA_CLAUDE_VERSION", "").strip()
    if configured:
        match = VERSION_PATTERN.search(configured)
        if match:
            return f"claude-code/{match.group()}"

    executable = shutil.which("claude")
    if executable:
        resolved = Path(executable).resolve()
        for package_path in (
            resolved.parent / "package.json",
            resolved.parent.parent / "package.json",
        ):
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
                version = package.get("version")
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(version, str) and VERSION_PATTERN.fullmatch(version):
                return f"claude-code/{version}"
        for path_part in reversed(resolved.parts):
            if VERSION_PATTERN.fullmatch(path_part):
                return f"claude-code/{path_part}"

    return "claude-code/unknown"


def credential() -> str:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token

    pi_credential = read_pi_credential("anthropic")
    if pi_credential:
        value, path = pi_credential
        if value.get("type") != "oauth":
            raise RuntimeError(f"invalid anthropic credential in {path}")
        return required_string(value, "access", path)

    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    path = config / ".credentials.json"
    credentials = read_credentials(path)
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise RuntimeError(f"Claude OAuth credentials not found in {path}")
    return required_string(oauth, "accessToken", path)


def extract_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rate_limits = payload.get("rate_limits")
    statusline_shape = isinstance(rate_limits, dict)
    source = rate_limits if statusline_shape else payload
    windows = []
    for key, minutes in (("five_hour", 300), ("seven_day", 10080)):
        candidate = source.get(key)
        if not isinstance(candidate, dict):
            continue
        used_key = "used_percentage" if statusline_shape else "utilization"
        window = normalized_window(minutes, candidate.get(used_key), candidate.get("resets_at"))
        if window:
            windows.append(window)
    return complete_windows(windows)


def refresh(timeout: float = 12.0) -> None:
    token = credential()
    payload = fetch_json(
        USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "User-Agent": user_agent(),
            "x-app": "cli",
        },
        timeout,
    )
    windows = extract_windows(payload)
    if not windows:
        raise RuntimeError("Claude returned no quota windows")
    write_snapshot("claude", windows)
