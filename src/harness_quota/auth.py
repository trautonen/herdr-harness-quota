from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any


def read_credentials(path: Path) -> dict[str, Any]:
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.getuid():
            raise RuntimeError(f"unsafe credential file: {path}")
        with path.open(encoding="utf-8") as credential_file:
            payload = json.load(credential_file)
    except FileNotFoundError as error:
        raise RuntimeError(f"credential file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid credential file: {path}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid credential file: {path}")
    return payload


def required_string(container: dict[str, Any], key: str, path: Path) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"credential {key} not found in {path}")
    return value.strip()


def optional_string(container: dict[str, Any], key: str) -> str | None:
    value = container.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def pi_auth_path() -> Path:
    agent_directory = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    return agent_directory / "auth.json"


def read_pi_credential(provider: str) -> tuple[dict[str, Any], Path] | None:
    path = pi_auth_path()
    try:
        credentials = read_credentials(path)
    except RuntimeError as error:
        if not path.exists():
            return None
        raise error
    credential = credentials.get(provider)
    if credential is None:
        return None
    if not isinstance(credential, dict):
        raise RuntimeError(f"invalid {provider} credential in {path}")
    return credential, path
