"""Reads the GuardIA thresholds published to SSM Parameter Store.

`infra/config.py` defines the parameter names and defaults; handlers never
hardcode them (tech.md). Values are cached per Lambda container.
"""

from __future__ import annotations

import os
import time
from typing import Any

DEFAULT_SSM_PREFIX = "/guardia/config"
_CACHE_SECONDS = 300
_cache: dict[str, Any] = {"values": None, "loaded_at": 0.0, "prefix": None}


class MissingThresholdError(KeyError):
    """A required threshold is not published in SSM."""


def load_thresholds(ssm_client: Any, prefix: str | None = None, *, force: bool = False) -> dict:
    prefix = (prefix or os.environ.get("GUARDIA_SSM_PREFIX", DEFAULT_SSM_PREFIX)).rstrip("/")
    fresh = time.monotonic() - _cache["loaded_at"] < _CACHE_SECONDS
    if not force and _cache["values"] is not None and _cache["prefix"] == prefix and fresh:
        return _cache["values"]

    values: dict[str, str] = {}
    paginator = ssm_client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=False):
        for param in page["Parameters"]:
            values[param["Name"].rsplit("/", 1)[-1]] = param["Value"]

    _cache.update(values=values, loaded_at=time.monotonic(), prefix=prefix)
    return values


def get_threshold(thresholds: dict, name: str) -> str:
    try:
        return thresholds[name]
    except KeyError as exc:
        raise MissingThresholdError(f"SSM threshold '{name}' is not published") from exc


def clear_cache() -> None:
    _cache.update(values=None, loaded_at=0.0, prefix=None)
