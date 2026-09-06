"""Domain-scoped, rotating operational logs using Python's logging library."""
from ish.compat import is_junction
from typing import Optional

import json
import logging
import re
import warnings
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _RaisingFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The caller emits a safe warning without the record or exception text.
        raise OSError("Operational log write failed")


def log_event(logs: Path, event: str, *, entity_id: Optional[str] = None,
              related_id: Optional[str] = None, status: Optional[str] = None,
              count: Optional[int] = None, permanent: Optional[bool] = None) -> None:
    """Write only allowlisted operational fields; never accept arbitrary metadata.

    No global logger/handler cache and no open handle survives this call. This
    allows Windows domain deletion and avoids duplicate handlers. Logs are best
    effort, not a durable audit transaction. The domain root must already exist.
    """
    if not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", event):
        raise ValueError("Invalid operational event name")
    if not logs.parent.is_dir():
        return
    payload: dict = {"time": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, identifier in (("entity_id", entity_id), ("related_id", related_id)):
        if identifier is not None and re.fullmatch(r"[a-f0-9]{32}", identifier):
            payload[key] = identifier
    if status in {"idle", "running", "deleted", "queued", "committed", "streaming",
                  "pending", "completed", "interrupted", "cancelled", "failed"}:
        payload["status"] = status
    if type(count) is int and count >= 0:
        payload["count"] = count
    if type(permanent) is bool:
        payload["permanent"] = permanent
    handler = None
    logger = logging.Logger("ish.service", level=logging.INFO)
    logger.propagate = False
    try:
        for directory in (logs, *logs.parents):
            if directory.is_symlink() or is_junction(directory):
                raise OSError("Linked log directory")
        logs.mkdir(mode=0o700, exist_ok=True)
        path = logs / "service.log"
        for candidate in (path, *(logs / f"service.log.{i}" for i in range(1, 4))):
            if candidate.is_symlink() or is_junction(candidate):
                raise OSError("Linked log file")
        handler = _RaisingFileHandler(path, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        level = logging.ERROR if event.endswith(".failed") else logging.INFO
        logger.log(level, json.dumps(payload, ensure_ascii=False))
    except OSError:
        warnings.warn("ish could not write an operational log", RuntimeWarning, stacklevel=2)
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
