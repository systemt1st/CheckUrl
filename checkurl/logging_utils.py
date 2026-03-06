from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .utils import now_iso


def configure_logging(level: str, log_file: str = "") -> logging.Logger:
    logger = logging.getLogger("checkurl")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    formatter = logging.Formatter("%(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    payload = {"ts": now_iso(), "event": event, **fields}
    logger.log(level, json.dumps(payload, ensure_ascii=False))
