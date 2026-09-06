"""Resolve credentials at execution time without writing their values to disk."""

import os
import re
from typing import Protocol


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class SecretManager:
    """Minimal environment-backed resolver. References use env:VARIABLE_NAME."""

    def resolve(self, reference: str) -> str:
        if not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", reference):
            raise ValueError("Unsupported credential reference")
        value = os.environ.get(reference[4:])
        if not value:
            raise ValueError("Credential reference is unavailable")
        return value
