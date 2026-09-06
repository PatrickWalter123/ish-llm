"""Build conversation context for Runs and clones, independent of storage."""

from copy import deepcopy

from ish.core.models import Message, MessageRole, MessageStatus


class ConversationContextBuilder:
    def _ordered(self, messages: list[Message]) -> list[Message]:
        inputs = {message.run_id: index for index, message in enumerate(messages)
                  if message.role == MessageRole.USER and message.run_id}
        return [message for _, message in sorted(enumerate(messages), key=lambda item: (
            inputs.get(item[1].run_id, item[0]),
            item[1].role == MessageRole.ASSISTANT, item[0],
        ))]

    def for_run(self, messages: list[Message], input_message_id: str) -> tuple[Message, ...]:
        history = []
        committed = {message.run_id for message in messages
                     if message.role == MessageRole.USER and message.status == MessageStatus.COMMITTED}
        for message in self._ordered(messages):
            if message.id == input_message_id:
                history.append(message)
                return tuple(deepcopy(history))
            if message.role == MessageRole.USER:
                if message.status == MessageStatus.COMMITTED:
                    history.append(message)
            elif message.status in (MessageStatus.COMPLETED, MessageStatus.INTERRUPTED,
                                    MessageStatus.FAILED):
                if message.run_id is None or message.run_id in committed:
                    history.append(message)
        raise ValueError("Run input message is missing from the conversation")

    def for_clone(self, messages: list[Message]) -> list[Message]:
        snapshot = deepcopy(self._ordered(messages))
        for message in snapshot:
            message.run_id = None
            if message.status == MessageStatus.QUEUED:
                message.status = MessageStatus.CANCELLED
            elif message.status == MessageStatus.STREAMING:
                message.status = MessageStatus.INTERRUPTED
        return snapshot
