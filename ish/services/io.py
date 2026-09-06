"""Ordered, bounded filesystem work outside the asyncio event loop."""

import asyncio

from .locking import WorkspaceOwnership


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
