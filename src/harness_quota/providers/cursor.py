from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any

from ..auth import (
    decode_jwt_payload,
    optional_string,
    read_credentials,
    read_pi_credential,
    required_string,
)
from ..model import complete_windows, normalized_window, numeric_value
from ..storage import write_snapshot
from ..transport import fetch_json

API_URL = "https://api2.cursor.sh"
EXCHANGE_URL = f"{API_URL}/auth/exchange_user_api_key"
REFRESH_URL = f"{API_URL}/oauth/token"
USAGE_URL = f"{API_URL}/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"


def cli_auth_path() -> Path:
    configured = os.environ.get("CURSOR_CLI_CONFIG") or os.environ.get("CURSOR_AUTH_FILE")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / ".cursor" / "auth.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cursor" / "auth.json"


def state_db_path() -> Path | None:
    configured = os.environ.get("CURSOR_STATE_DB")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "globalStorage"
            / "state.vscdb"
        )
    if sys.platform.startswith("linux"):
        return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return None


def read_desktop_tokens(path: Path) -> dict[str, str]:
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.getuid():
            raise RuntimeError(f"unsafe credential database: {path}")
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = database.execute(
                "SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
                ("cursorAuth/accessToken", "cursorAuth/refreshToken"),
            ).fetchall()
        finally:
            database.close()
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError(f"could not read Cursor credential database: {path}") from error
    values = {key: value for key, value in rows if isinstance(value, str) and value.strip()}
    return {
        "accessToken": values.get("cursorAuth/accessToken", "").strip(),
        "refreshToken": values.get("cursorAuth/refreshToken", "").strip(),
    }


def credentials() -> dict[str, str]:
    pi_credential = read_pi_credential("cursor")
    if pi_credential:
        value, path = pi_credential
        if value.get("type") != "api_key":
            raise RuntimeError(f"invalid cursor credential in {path}")
        return {"apiKey": required_string(value, "key", path)}

    environment_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if environment_key:
        return {"apiKey": environment_key}

    sdk_path = Path.home() / ".cursor" / "sdk" / "auth.json"
    if sdk_path.exists():
        sdk_credentials = read_credentials(sdk_path)
        return {"apiKey": required_string(sdk_credentials, "apiKey", sdk_path)}

    cli_path = cli_auth_path()
    if cli_path.exists():
        cli_credentials = read_credentials(cli_path)
        tokens = {
            key: value
            for key in ("accessToken", "refreshToken", "apiKey")
            if (value := optional_string(cli_credentials, key))
        }
        if tokens:
            return tokens
        raise RuntimeError(f"Cursor credentials not found in {cli_path}")

    database_path = state_db_path()
    if database_path and database_path.exists():
        tokens = {key: value for key, value in read_desktop_tokens(database_path).items() if value}
        if tokens:
            return tokens
        raise RuntimeError(f"Cursor credentials not found in {database_path}")
    raise RuntimeError(f"credential file not found: {cli_path}")


def total_percent(plan_usage: dict[str, Any]) -> float | None:
    reported = numeric_value(plan_usage.get("totalPercentUsed"))
    if reported is not None:
        return reported
    limit = numeric_value(plan_usage.get("limit"))
    if limit is None or limit <= 0:
        return None
    remaining = numeric_value(plan_usage.get("remaining"))
    if remaining is not None:
        return (limit - remaining) / limit * 100
    for key in ("includedSpend", "totalSpend", "used"):
        spent = numeric_value(plan_usage.get(key))
        if spent is not None:
            return spent / limit * 100
    return None


def extract_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    plan_usage = payload.get("planUsage")
    if not isinstance(plan_usage, dict):
        return []
    reset = payload.get("billingCycleEnd")
    total_is_reported = numeric_value(plan_usage.get("totalPercentUsed")) is not None
    windows = []
    for minutes, used, estimated in (
        (300, plan_usage.get("autoPercentUsed"), True),
        (10080, plan_usage.get("apiPercentUsed"), True),
        (43200, total_percent(plan_usage), not total_is_reported),
    ):
        window = normalized_window(minutes, numeric_value(used), reset, estimated)
        if window:
            windows.append(window)
    return complete_windows(windows)


def access_token(current_credentials: dict[str, str], timeout: float) -> str:
    token = current_credentials.get("accessToken")
    payload = decode_jwt_payload(token) if token else None
    expires_at = numeric_value(payload.get("exp")) if payload else None
    if token and (expires_at is None or expires_at > time.time() + 300):
        return token

    api_key = current_credentials.get("apiKey")
    if api_key:
        exchange = fetch_json(
            EXCHANGE_URL,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout,
            method="POST",
            body=b"{}",
        )
        return required_string(exchange, "accessToken", Path("Cursor API key exchange"))

    refresh_token = current_credentials.get("refreshToken")
    if refresh_token:
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            separators=(",", ":"),
        ).encode()
        refreshed = fetch_json(
            REFRESH_URL,
            {"Content-Type": "application/json"},
            timeout,
            method="POST",
            body=body,
        )
        return required_string(refreshed, "access_token", Path("Cursor OAuth refresh"))

    if token:
        return token
    raise RuntimeError("no usable Cursor credential")


def refresh(timeout: float = 12.0) -> None:
    token = access_token(credentials(), timeout)
    payload = fetch_json(
        USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        timeout,
        method="POST",
        body=b"{}",
    )
    windows = extract_windows(payload)
    if not windows:
        raise RuntimeError("Cursor returned no quota windows")
    write_snapshot("cursor", windows)
