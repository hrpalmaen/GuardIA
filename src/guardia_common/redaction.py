"""Redaction and size limits for error causes and execution data.

The Genesys extraction processes carry OAuth access tokens inside their state
input, so an error cause can contain them (a real `States.Runtime` cause embeds
the whole state input: ~22 KB, with tokens). Everything GuardIA stores,
classifies, sends to Bedrock, or posts to Teams goes through `sanitize_cause`.

The same functions sanitize the local fixtures (`scripts/sanitize_executions.py`),
so local data and production data are cleaned by identical rules.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "REDACTED"
MAX_CAUSE_CHARS = 2000  # technical cap for storage and messages, not a business threshold

# JSON keys whose values are credentials.
_SECRET_KEY = re.compile(
    r"^(access_?tokens?|refresh_?token|id_?token|token|client_?secret|secret|password"
    r"|authorization|api_?key)$",
    re.IGNORECASE,
)
# Key names to redact, not a secret.
_SECRET_KEY_SOURCE = (
    r"access_?tokens?|refresh_?token|id_?token|client_?secret|password|authorization|api_?key"  # noqa: S105
)
# "accessToken": "..."  or  "accessTokens": [ ... ]  inside free text.
_SECRET_PAIR = re.compile(
    rf'("(?:{_SECRET_KEY_SOURCE})"\s*:\s*)("(?:[^"\\]|\\.)*"|\[[^\]]*\])', re.IGNORECASE
)
# The same pair inside a JSON string that is itself escaped: \"accessToken\":\"...\"
_ESCAPED_SECRET_PAIR = re.compile(
    rf'(\\"(?:{_SECRET_KEY_SOURCE})\\"\s*:\s*)(\\"(?:(?!\\").)*\\"|\[[^\]]*\])', re.IGNORECASE
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
# Opaque tokens: long base64url runs with both upper and lower case letters. Execution
# names and request ids are lowercase hex, so they are not affected.
_OPAQUE_TOKEN = re.compile(r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])[A-Za-z0-9_-]{60,}")
# Step Functions runtime errors quote the whole state input or output.
_EMBEDDED_DATA = re.compile(r"(\b(?:input|output)\s*')\{.*\}(')", re.DOTALL)


def redact_text(text: str) -> str:
    """Removes credentials from free text without changing its structure."""
    text = _SECRET_PAIR.sub(lambda m: f'{m.group(1)}"{REDACTED}"', text)
    text = _ESCAPED_SECRET_PAIR.sub(lambda m: f'{m.group(1)}\\"{REDACTED}\\"', text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    return _OPAQUE_TOKEN.sub(REDACTED, text)


def redact_value(value: Any) -> Any:
    """Recursively redacts a JSON-like value. Strings that contain JSON documents
    (Step Functions passes input and output as JSON strings) are redacted inside
    and re-serialized compactly."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if _SECRET_KEY.match(k) and v not in (None, "") else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                return redact_text(value)
            return json.dumps(redact_value(parsed), ensure_ascii=False, separators=(",", ":"))
        return redact_text(value)
    return value


def truncate(text: str, max_chars: int = MAX_CAUSE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}…[truncated {omitted} chars]"


def sanitize_cause(text: str | None, max_chars: int = MAX_CAUSE_CHARS) -> str | None:
    """Cause ready to store, classify, or send: no embedded state data, no
    credentials, bounded size. Removing the embedded input also prevents the
    catalog from matching numbers in the payload (e.g. `"totalHits":403`)."""
    if text is None:
        return None
    text = _EMBEDDED_DATA.sub(r"\1<omitted>\2", text)
    return truncate(redact_text(text), max_chars)
