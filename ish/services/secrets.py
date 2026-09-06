"""Resolve credentials at execution time without writing their values to disk."""

import os
import re
from pathlib import Path
from typing import Protocol
from .logging import log_event


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class SecretManager:
    """Minimal environment-backed resolver. References use env:VARIABLE_NAME."""

    def __init__(self, *, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir

    def resolve(self, reference: str) -> str:
        if not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", reference):
            self._log("secret.failed")
            raise ValueError("Unsupported credential reference")
        value = os.environ.get(reference[4:])
        if not value:
            self._log("secret.failed")
            raise ValueError("Credential reference is unavailable")
        self._log("secret.resolved")
        return value

    def _log(self, event: str) -> None:
        if self.log_dir is not None:
            log_event(self.log_dir, event)
