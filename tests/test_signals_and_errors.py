"""Process signal handlers across overlapping servers, and which error a failing test reports."""

import asyncio
import signal
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from types import FrameType

import pytest
from fastapi import FastAPI

from fastapi_testing import AsyncTestServer, ServerShutdownError, create_test_server


@pytest.fixture
def recorded_signals() -> Iterator[list[int]]:
    """Install recording SIGINT/SIGTERM handlers as the process's originals, restore afterwards."""
    received: list[int] = []

    def record(sig: int, frame: FrameType | None) -> None:
        received.append(sig)

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in saved:
        signal.signal(sig, record)
    try:
        yield received
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _handler_server(sig: int = signal.SIGTERM) -> object:
    """The server whose bound ``handle_exit`` is installed (what sse-starlette looks up)."""
    return getattr(signal.getsignal(sig), "__self__", None)


async def _wait_for(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["first-started-first", "last-started-first", "concurrently"])
async def test_original_handlers_restored_whatever_the_stop_order(recorded_signals: list[int], order: str):
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    a, b = AsyncTestServer(), AsyncTestServer()
    await a.start()
    await b.start()
    if order == "first-started-first":
        await a.stop()
        await b.stop()
    elif order == "last-started-first":
        await b.stop()
        await a.stop()
    else:
        await asyncio.gather(a.stop(), b.stop())

    assert {sig: signal.getsignal(sig) for sig in originals} == originals
    # Ctrl-C reaches the original handler instead of a stopped server's handle_exit.
    signal.raise_signal(signal.SIGINT)
    assert recorded_signals == [signal.SIGINT]


@pytest.mark.asyncio
async def test_installed_handler_belongs_to_a_running_server(recorded_signals: list[int]):
    a, b = AsyncTestServer(), AsyncTestServer()
    await a.start()
    await b.start()
    try:
        assert _handler_server() is b._server
        await a.stop()
        assert _handler_server() is b._server
        c = AsyncTestServer()
        await c.start()
        assert _handler_server() is c._server
        await c.stop()
        # Back to the server still running, not to a stopped one.
        assert _handler_server() is b._server
        assert not getattr(_handler_server(), "should_exit", True)
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_signal_stops_every_running_server_then_reaches_the_original_handler(recorded_signals: list[int]):
    a, b = AsyncTestServer(), AsyncTestServer()
    await a.start()
    await b.start()
    try:
        tasks = [a._server_task, b._server_task]
        signal.raise_signal(signal.SIGINT)
        # The newest server handles it, stops, and raises it again for the next one;
        # after the last, the original handler receives it exactly once.
        await _wait_for(lambda: recorded_signals == [signal.SIGINT])
        await _wait_for(lambda: all(t is not None and t.done() for t in tasks))
    finally:
        await a.stop()
        await b.stop()
    assert recorded_signals == [signal.SIGINT]


def _failing_shutdown_lifespan():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        yield
        raise ValueError("shutdown broke")

    return lifespan


@pytest.mark.asyncio
async def test_create_test_server_keeps_the_test_failure_over_a_shutdown_error():
    with pytest.raises(AssertionError, match="the test's own failure") as info:
        async with create_test_server(lifespan=_failing_shutdown_lifespan()):
            raise AssertionError("the test's own failure")
    notes = getattr(info.value, "__notes__", [])
    assert any("stopping the server also failed" in n and "shutdown broke" in n for n in notes), notes


@pytest.mark.asyncio
async def test_server_context_manager_keeps_the_test_failure_over_a_shutdown_error():
    with pytest.raises(KeyError) as info:
        async with AsyncTestServer(lifespan=_failing_shutdown_lifespan()):
            raise KeyError("missing")
    assert any("shutdown broke" in n for n in getattr(info.value, "__notes__", []))


@pytest.mark.asyncio
async def test_shutdown_error_still_raised_when_the_test_passed():
    with pytest.raises(ServerShutdownError) as info:
        async with create_test_server(lifespan=_failing_shutdown_lifespan()):
            pass
    assert isinstance(info.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_cancellation_of_the_test_is_not_replaced():
    async def body() -> None:
        async with create_test_server(lifespan=_failing_shutdown_lifespan()):
            await asyncio.sleep(30)

    task = asyncio.create_task(body())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
