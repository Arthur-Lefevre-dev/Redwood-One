"""Structured JSON logging helper."""

import json
import logging
from typing import Any, Dict


def log_event(logger: logging.Logger, step: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"step": step, **fields}
    logger.info(json.dumps(payload, default=str))
