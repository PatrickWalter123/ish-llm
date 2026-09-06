"""Shared filesystem operations and ordered background storage execution."""

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import fields
from pathlib import Path

from ish.compat import is_junction
from .locking import WorkspaceOwnership


# Metadata serialization and durable filesystem primitives.

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


# Validated permanent removal of an owned domain directory.

def remove_owned_tree(owner: Path, target: Path, identifier: str) -> None:
    """Delete one identified child, rejecting path escapes and linked contents.

    This preflight assumes exclusive workspace ownership; it is not a defense
    against another process swapping paths between validation and removal.
    """
    expected = child(owner, identifier).absolute()
    if target.absolute() != expected:
        raise ValueError("Deletion target does not match the owned object")
    # Reject redirected ancestors too, including Windows junctions.
    for path in (target, *target.parents):
        if path.is_symlink() or is_junction(path):
            raise ValueError("Deletion through linked paths is not allowed")
    resolved_owner = owner.resolve(strict=True)
    resolved_target = target.resolve(strict=True)
    if resolved_target.parent != resolved_owner or resolved_target == resolved_owner:
        raise ValueError("Deletion target escapes its owner")
    if not resolved_target.is_dir():
        raise ValueError("Deletion target must be a directory")

    def fail(error: OSError) -> None:
        raise error

    for directory, names, files in os.walk(resolved_target, followlinks=False, onerror=fail):
        for name in (*names, *files):
            entry = Path(directory) / name
            if entry.is_symlink() or is_junction(entry):
                raise ValueError("Remove linked contents before permanent deletion")
    # Absolute target and containment have been verified above.
    shutil.rmtree(resolved_target)
    sync_directory(resolved_owner)


# Ordered background storage and cancellation draining.

class StorageIO:
    """One in-flight transaction per manager, no unbounded executor backlog.

    Cancellation drains the submitted operation before propagating. A worker
    thread cannot be forcibly stopped midway through a durable transaction.
    """

    def __init__(self, ownership: WorkspaceOwnership) -> None:
        self.ownership = ownership
        self._serial = None

    async def run(self, operation, *args, **kwargs):
        if self._serial is None:
            self._serial = asyncio.Lock()
        async with self._serial:
            def work():
                with self.ownership.scope():
                    return operation(*args, **kwargs)

            pending = asyncio.ensure_future(asyncio.to_thread(work))
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            # Surface storage failures even if cancellation arrived meanwhile.
            result = pending.result()
            if cancelled:
                raise asyncio.CancelledError
            return result


async def drain_on_cancel(awaitable):
    """Complete an accepted orchestration action before forwarding cancellation."""
    pending = asyncio.ensure_future(awaitable)
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    result = pending.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
