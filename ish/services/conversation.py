from typing import Optional
import json
import os
import threading
from functools import wraps
from copy import deepcopy
from pathlib import Path

from ish.core.models import Message, MessageRole, MessageStatus, Task, new_id
from ish.core.paths import TaskPaths
from .logging import log_event
from .storage import record, sync_directory


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return guarded


class ConversationStore:
    """Incremental JSONL projection; caller owns workspace write coordination."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mutex = threading.RLock()
        self._messages: dict[str, Message] = {}
        self._offset = 0
        self._signature = None

    def _repair_tail(self) -> None:
        # A crash can leave an incomplete last append. Never discard a full line.
        if not self.path.exists():
            return
        with self.path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            if not end:
                return
            stream.seek(end - 1)
            if stream.read(1) == b"\n":
                return
            position = end
            while position:
                start = max(0, position - 8192)
                stream.seek(start)
                block = stream.read(position - start)
                newline = block.rfind(b"\n")
                if newline != -1:
                    stream.truncate(start + newline + 1)
                    break
                position = start
            else:
                stream.truncate(0)
            stream.flush()
            os.fsync(stream.fileno())

    def _append(self, event: dict) -> None:
        data = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._signature is not None and self._offset < self._signature[2]:
            self._repair_tail()
        with self.path.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if self._signature is None:
            sync_directory(self.path.parent)
        # Apply the serialized snapshot only after fsync succeeds.
        self._apply(json.loads(data))
        stat = self.path.stat()
        self._offset = stat.st_size
        self._signature = self._stamp(stat)
        # Text already has a durable JSONL record. Avoid a second file open and
        # rotation check for every token; lifecycle events remain operational logs.
        if event["type"] != "message.delta":
            message = event.get("message", {})
            log_event(TaskPaths(self.path.parent).logs, event["type"],
                      entity_id=event.get("id", message.get("id")),
                      status=event.get("status", message.get("status")))

    @staticmethod
    def _stamp(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _apply(self, event: dict) -> None:
        kind = event["type"]
        if kind == "message.create":
            data = event["message"]
            if data["id"] in self._messages:
                raise ValueError("Duplicate message ID")
            self._messages[data["id"]] = Message(
                **{**data, "role": MessageRole(data["role"]),
                   "status": MessageStatus(data["status"])})
        else:
            message = self._messages[event["id"]]
            if kind == "message.delta":
                message.content += event["text"]
            elif kind == "message.status":
                message.status = MessageStatus(event["status"])
            elif kind == "message.metadata":
                message.metadata.update(event["metadata"])
            elif kind == "message.run":
                message.run_id = event["run_id"]
            else:
                raise ValueError(f"Unknown conversation event: {kind}")

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._messages.clear()
            self._offset, self._signature = 0, None
            return
        stamp = self._stamp(stat)
        if stamp == self._signature:
            return
        previous = self._signature
        if (previous is None or stamp[:2] != previous[:2]
                or stat.st_size < self._offset
                or (stat.st_size <= previous[2] and stamp != previous)):
            self._messages.clear()
            self._offset = 0
        try:
            with self.path.open("rb") as stream:
                stream.seek(self._offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    self._apply(json.loads(line))
                    self._offset += len(line)
            self._signature = stamp
        except BaseException:
            # A failed replay must fail again on the next read, not return a
            # partially projected cache as if the corrupt record were valid.
            self._messages.clear()
            self._offset, self._signature = 0, None
            raise

    @_serialized
    def list(self) -> list[Message]:
        self._refresh()
        return deepcopy(list(self._messages.values()))

    @_serialized
    def get(self, message_id: str) -> Message:
        self._refresh()
        return deepcopy(self._messages[message_id])

    @_serialized
    def create(self, role: MessageRole, content: str, status: MessageStatus,
               *, message_id: Optional[str] = None, run_id: Optional[str] = None,
               metadata: Optional[dict] = None) -> Message:
        message = Message(message_id or new_id(), role, content, status,
                          run_id=run_id, metadata=deepcopy(metadata or {}))
        self._refresh()
        if message.id in self._messages:
            raise ValueError("Duplicate message ID")
        self._append({"type": "message.create", "message": record(message)})
        return message

    @_serialized
    def delta(self, message_id: str, text: str) -> None:
        self._refresh()
        if self._messages[message_id].status != MessageStatus.STREAMING:
            raise ValueError("Deltas require a streaming message")
        self._append({"type": "message.delta", "id": message_id, "text": text})

    @_serialized
    def set_status(self, message_id: str, status: MessageStatus) -> None:
        self._refresh()
        self._messages[message_id]
        self._append({"type": "message.status", "id": message_id, "status": status})

    @_serialized
    def bind_run(self, message_id: str, run_id: str) -> None:
        self._refresh()
        self._messages[message_id]
        self._append({"type": "message.run", "id": message_id, "run_id": run_id})

    @_serialized
    def update_metadata(self, message_id: str, metadata: dict) -> None:
        self._refresh()
        self._messages[message_id]
        self._append({"type": "message.metadata", "id": message_id, "metadata": metadata})


def conversation_store(task: Task) -> ConversationStore:
    """Default injectable factory; consumers do not choose storage paths."""
    return ConversationStore(task.paths.conversation)
