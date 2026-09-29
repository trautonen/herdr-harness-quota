from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..auth import decode_jwt_payload, read_credentials, read_pi_credential, required_string
from ..history import record_snapshot
from ..model import complete_windows, normalized_window
from ..transport import fetch_json

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def account_id(token: str, stored: Any) -> str | None:
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    payload = decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth") if payload else None
    value = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def credential() -> tuple[str, str | None]:
    pi_credential = read_pi_credential("openai-codex")
    if pi_credential:
        value, path = pi_credential
        if value.get("type") != "oauth":
            raise RuntimeError(f"invalid openai-codex credential in {path}")
        token = required_string(value, "access", path)
        return token, account_id(token, value.get("accountId"))

    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    credentials = read_credentials(path)
    tokens = credentials.get("tokens")
    if not isinstance(tokens, dict):
        raise RuntimeError(f"Codex OAuth credentials not found in {path}")
    token = required_string(tokens, "access_token", path)
    stored_account_id = tokens.get("account_id", credentials.get("account_id"))
    return token, account_id(token, stored_account_id)


def extract_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rate_limit = payload.get("rate_limit")
    source = rate_limit if isinstance(rate_limit, dict) else payload.get("rateLimits")
    if not isinstance(source, dict):
        source = payload
    windows = []
    for names in (("primary_window", "primary"), ("secondary_window", "secondary")):
        candidate = next(
            (source.get(name) for name in names if isinstance(source.get(name), dict)), None
        )
        if not isinstance(candidate, dict):
            continue
        minutes = candidate.get("windowDurationMins")
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            seconds = candidate.get("limit_window_seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                continue
            minutes = round(seconds / 60)
        window = normalized_window(
            minutes,
            candidate.get("usedPercent", candidate.get("used_percent")),
            candidate.get("resetsAt", candidate.get("reset_at")),
        )
        if window:
            windows.append(window)
    return complete_windows(windows)


def refresh(timeout: float = 12.0) -> None:
    token, current_account_id = credential()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
    if current_account_id:
        headers["ChatGPT-Account-Id"] = current_account_id
    payload = fetch_json(USAGE_URL, headers, timeout)
    windows = extract_windows(payload)
    if not windows:
        raise RuntimeError("Codex returned no quota windows")
    record_snapshot("codex", windows)
