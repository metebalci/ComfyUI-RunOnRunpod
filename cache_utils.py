"""Shared TTL-based JSON cache helpers used by ``latency.py`` and
``model_lookup.py``. Both modules pull rarely-changing data from external
sources (Runpod docs page, ComfyUI-Manager model DB) and want identical
semantics: read fresh cache, fall back to a network fetch, then fall back
to a stale cache on fetch failure.
"""

import json
import os
import time
from typing import Any, Optional

_PREFIX = "[RunOnRunpod]"


def plugin_cache_dir() -> str:
    """Return (and create) the plugin-local cache directory."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def read_json_cache(cache_path: str, ttl: float) -> Optional[Any]:
    """Return parsed JSON if the cache file exists and is fresher than
    ``ttl`` seconds old. Returns None on miss, stale, or parse error.
    """
    if not os.path.exists(cache_path):
        return None
    if time.time() - os.path.getmtime(cache_path) >= ttl:
        return None
    try:
        with open(cache_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def read_stale_json_cache(cache_path: str) -> Optional[Any]:
    """Return parsed JSON ignoring TTL. For last-ditch fallback when a
    fresh fetch failed and we'd rather serve old data than nothing.
    """
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def write_json_cache(cache_path: str, data: Any) -> None:
    """Persist ``data`` as JSON. Failures are logged, not raised — caching
    is an optimization, not a correctness requirement.
    """
    try:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    except OSError as e:
        print(f"{_PREFIX} Failed to write cache {cache_path}: {e}")
