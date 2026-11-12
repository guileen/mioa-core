"""Explicit local exec authority with cancellation-safe child ownership."""
import asyncio
from collections.abc import Mapping, Sequence
from typing import TypeVar
from .leases import _Lease

T = TypeVar("T")


async def _settle(task: asyncio.Task[T]) -> T:
    """Keep ownership until work finishes, even through repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is not None:
        while await stream.read(65536):
            pass


async def _discard(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        # A concurrent exit is not itself exit evidence; still drain and wait.
        pass
    results = await asyncio.gather(
        process.wait(), _drain(process.stdout), _drain(process.stderr),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise BaseExceptionGroup("unclaimed child cleanup failed", failures)
    if process.stdin is not None:
        process.stdin.close()


class _LocalChildProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._exit = asyncio.create_task(self._observe_exit())

    async def _observe_exit(self) -> int:
        status = await self._process.wait()
        if self._process.stdin is not None:
            self._process.stdin.close()
        return status

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        return self._process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        return self._process.stderr

    async def wait(self) -> int:
        return await asyncio.shield(self._exit)

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()


class LocalChildProcesses:
    """Host-installed local process creation; not a sandbox or tree owner."""

    async def start(self, argv: Sequence[str], *, environment: Mapping[str, str],
                    profile_lease=None) -> _LocalChildProcess:
        arguments = tuple(argv)
        child_environment = dict(environment)
        descriptors = ()
        if profile_lease is not None:
            if not isinstance(profile_lease, _Lease) or not profile_lease._handles or profile_lease._scope[2] != "engine":
                raise ValueError("invalid or closed engine lease")
            descriptors = tuple(handle.fileno() for handle in profile_lease._handles)
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
            *arguments, env=child_environment, pass_fds=descriptors, close_fds=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ))
        try:
            process = await asyncio.shield(creation)
        except asyncio.CancelledError as cancellation:
            try:
                process = await _settle(creation)
            except BaseException as failure:
                cancellation.add_note(f"cancelled child creation failed: {type(failure).__name__}")
                raise cancellation from failure
            cleanup = asyncio.create_task(_discard(process))
            try:
                await _settle(cleanup)
            except BaseException as failure:
                cancellation.add_note(f"unclaimed child cleanup failed: {type(failure).__name__}")
                raise cancellation from failure
            raise
        return _LocalChildProcess(process)
