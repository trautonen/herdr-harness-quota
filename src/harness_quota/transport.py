from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def fetch_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    method: str = "GET",
    body: bytes | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"quota request failed with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"quota request failed: {error.reason}") from error
    except (TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("quota request returned no valid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("quota response must be a JSON object")
    return payload
