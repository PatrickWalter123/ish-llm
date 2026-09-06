"""Atomic metadata writes, with directory sync on the Linux target."""

import json
import os
import re
import tempfile
from dataclasses import fields
from pathlib import Path


def child(root: Path, identifier: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid object ID")
    return root / identifier


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    # Serialize before touching the destination, including on invalid metadata.
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def record(model: object) -> dict:
    return {item.name: getattr(model, item.name) for item in fields(model)
            if item.name != "paths"}
