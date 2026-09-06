"""Validate the full owned directory before destructive filesystem operations."""
from ish.compat import is_junction

import os
import shutil
from pathlib import Path

from .storage import child, sync_directory


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
