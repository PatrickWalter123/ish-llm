from typing import Optional
import json
import os
from copy import deepcopy
from pathlib import Path

from ish.core.models import Message, MessageRole, MessageStatus, new_id
from ish.core.paths import TaskPaths
from .logging import log_event
from .storage import record, sync_directory


class ConversationStore:
    """One event-loop writer per Task; newline-terminated events are durable records."""

    def __init__(self, path: Path) -> None:
        self.path = path

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
        self._repair_tail()
        with self.path.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.path.parent)
        message = event.get("message", {})
        log_event(TaskPaths(self.path.parent).logs, event["type"],
                  entity_id=event.get("id", message.get("id")),
                  status=event.get("status", message.get("status")))

    def list(self) -> list[Message]:
        messages: dict[str, Message] = {}
        if not self.path.exists():
            return []
        with self.path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                event = json.loads(line)
                kind = event["type"]
                if kind == "message.create":
                    data = event["message"]
                    if data["id"] in messages:
                        raise ValueError("Duplicate message ID")
                    messages[data["id"]] = Message(
                        **{**data, "role": MessageRole(data["role"]),
                           "status": MessageStatus(data["status"])})
                else:
                    message = messages[event["id"]]
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
        return list(messages.values())

    def get(self, message_id: str) -> Message:
        for message in self.list():
            if message.id == message_id:
                return message
        raise KeyError(message_id)

    def create(self, role: MessageRole, content: str, status: MessageStatus,
               *, message_id: Optional[str] = None, run_id: Optional[str] = None,
               metadata: Optional[dict] = None) -> Message:
        message = Message(message_id or new_id(), role, content, status,
                          run_id=run_id, metadata=deepcopy(metadata or {}))
        if any(existing.id == message.id for existing in self.list()):
            raise ValueError("Duplicate message ID")
        self._append({"type": "message.create", "message": record(message)})
        return message

    def delta(self, message_id: str, text: str) -> None:
        if self.get(message_id).status != MessageStatus.STREAMING:
            raise ValueError("Deltas require a streaming message")
        self._append({"type": "message.delta", "id": message_id, "text": text})

    def set_status(self, message_id: str, status: MessageStatus) -> None:
        self.get(message_id)
        self._append({"type": "message.status", "id": message_id, "status": status})

    def bind_run(self, message_id: str, run_id: str) -> None:
        self.get(message_id)
        self._append({"type": "message.run", "id": message_id, "run_id": run_id})

    def update_metadata(self, message_id: str, metadata: dict) -> None:
        self.get(message_id)
        self._append({"type": "message.metadata", "id": message_id, "metadata": metadata})
