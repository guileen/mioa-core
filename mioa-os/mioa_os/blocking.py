"""Thread completion remains owned even when the async caller is cancelled."""
import asyncio
from collections.abc import Callable
from contextvars import copy_context
from typing import TypeVar

T = TypeVar("T")


class LocalBlockingCalls:
    """Local executor for bounded cooperative operations; no thread termination."""

    async def run(self, operation: Callable[[], T], *, discard: Callable[[T], None] | None = None) -> T:
        # Use an executor Future, not a worker Task: Runner shutdown cancels all
        # Tasks, which must not make a still-running thread appear finished.
        worker = asyncio.get_running_loop().run_in_executor(None, copy_context().run, operation)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            # Repeated cancellation cannot transfer live worker ownership back
            # to the caller. Its lock/resources stay valid until this returns.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                result = worker.result()
            except BaseException as exc:
                cancellation.add_note(f"cancelled blocking operation ended with {type(exc).__name__}")
            else:
                if discard is not None:
                    try:
                        discard(result)
                    except BaseException as exc:
                        cancellation.add_note(f"cancelled blocking result cleanup failed: {type(exc).__name__}")
            raise
