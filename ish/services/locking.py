"""Cooperating services use one OS-backed owner per local workspace.

The lock file is permanent: unlinking it could create two independent locks.
OS handle lifetime, rather than a PID or timeout, determines ownership.
"""

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from ish.compat import is_junction


class WorkspaceBusyError(RuntimeError):
    """Another repository instance/process currently owns this workspace."""


class WorkspaceOwnership:
    def __init__(self, root: Path) -> None:
        self.path = root.absolute() / ".ish.lock"
        self._mutex = threading.RLock()
        self._stream = None
        self._references = 0
        self._pid = os.getpid()
        self._tasks = set()

    def claim_task(self, key) -> None:
        with self._mutex:
            if key in self._tasks:
                raise ValueError("Task already has an attached runtime")
            self.retain()
            self._tasks.add(key)

    def release_task(self, key) -> None:
        with self._mutex:
            self._tasks.remove(key)
            self.release()

    def task_attached(self, key) -> bool:
        with self._mutex:
            return key in self._tasks

    def retain(self) -> None:
        with self._mutex:
            if os.getpid() != self._pid:
                raise RuntimeError("Create a new ProjectRepository after fork")
            if not self._references:
                for candidate in (self.path, *self.path.parents):
                    if candidate.is_symlink() or is_junction(candidate):
                        raise ValueError("Linked workspace lock path")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stream = self.path.open("a+b")
                try:
                    if os.name == "nt":
                        import msvcrt
                        # Windows byte-range locks may extend beyond EOF.
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    elif os.name == "posix":
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        raise RuntimeError("Unsupported workspace lock platform")
                except OSError as error:
                    stream.close()
                    raise WorkspaceBusyError("Workspace is owned by another service instance") from error
                except BaseException:
                    stream.close()
                    raise
                self._stream = stream
            self._references += 1

    def release(self) -> None:
        with self._mutex:
            if not self._references:
                raise RuntimeError("Workspace ownership is not held")
            self._references -= 1
            if not self._references:
                self._stream.close()
                self._stream = None

    @contextmanager
    def scope(self):
        # Serializes synchronous transactions across threads sharing this owner.
        with self._mutex:
            self.retain()
            try:
                yield
            finally:
                self.release()


def workspace_locked(method):
    """Hold ownership throughout a synchronous service transaction."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.ownership.scope():
            return method(self, *args, **kwargs)
    return guarded
