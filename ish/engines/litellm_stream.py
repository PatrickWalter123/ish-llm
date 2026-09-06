"""Adapt synchronous LiteLLM streaming without blocking the application's loop."""

import asyncio
import concurrent.futures
import inspect
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any


class StreamError(RuntimeError):
    """A sanitized provider/transport failure; no provider exception payload."""


def completion(**kwargs: Any) -> Iterator[Any]:
    # Import and request creation both happen in the stream's worker thread.
    import litellm

    return litellm.completion(**kwargs)


async def stream_completion(
    request: dict[str, Any], *, completion_fn: Callable[..., Iterator[Any]] = completion,
    buffer_size: int = 8,
) -> AsyncIterator[Any]:
    """One bounded bridge per request, with stream ownership in one thread.

    Cancellation stops delivery immediately. A blocked synchronous call cannot
    be forcibly cancelled: the daemon worker closes its stream when the call
    returns or its provider timeout expires. It never executes tools or persists.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=buffer_size)
    stopped = threading.Event()

    def send(kind: str, value: Any = None) -> bool:
        if stopped.is_set():
            return False
        pending = queue.put((kind, value))
        try:
            future = asyncio.run_coroutine_threadsafe(pending, loop)
        except RuntimeError:
            pending.close()
            return False
        while not stopped.is_set():
            try:
                future.result(timeout=0.05)
                return True
            except concurrent.futures.TimeoutError:
                continue
            except (concurrent.futures.CancelledError, RuntimeError):
                return False
        future.cancel()
        return False

    def produce() -> None:
        stream = None
        failed = False
        try:
            if stopped.is_set():
                return
            stream = completion_fn(**request)
            iterator = iter(stream)
            while not stopped.is_set():
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                if not send("chunk", chunk):
                    break
        except Exception:
            failed = True
        finally:
            # Cleanup is never performed concurrently with next(stream).
            if stream is not None:
                try:
                    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                    if close is not None:
                        result = close()
                        if inspect.isawaitable(result):
                            asyncio.run(result)
                except Exception:
                    failed = True
            if not stopped.is_set():
                send("error" if failed else "end")

    threading.Thread(target=produce, name="ish-litellm-stream", daemon=True).start()
    try:
        while True:
            kind, value = await queue.get()
            if kind == "end":
                return
            if kind == "error":
                raise StreamError("LLM stream failed")
            yield value
    finally:
        stopped.set()
