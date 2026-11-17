"""Structured JSON logger shared by every GuardIA Lambda.

Never pass webhook URLs, secrets, task tokens, error causes, or execution
inputs as fields: they can contain credentials or personal data.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    return logger


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})
