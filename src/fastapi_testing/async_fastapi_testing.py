import asyncio
import inspect
import json
import logging
import math
import os
import random
import signal
import socket
import sys
import threading
import tomllib
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any, Self

import httpx
import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Lifespan, Message, Receive, Scope, Send
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, InvalidMessage
from websockets.protocol import State

if sys.version_info >= (3, 12):
    from typing import override
else:  # pragma: no cover - Python 3.11 only; coverage is uploaded from the 3.13 job
    from typing_extensions import override

logger = logging.getLogger(__name__)

# Configuration Constants
DEFAULT_WS_MESSAGE_SIZE = 2**20  # 1MB
DEFAULT_WS_QUEUE_SIZE = 32
DEFAULT_KEEPALIVE_CONNS = 20
DEFAULT_MAX_CONNS = 100
DEFAULT_WS_RETRY_ATTEMPTS = 3
DEFAULT_WS_RETRY_DELAY = 1.0


class Config:
    """Global configuration settings for fastapi-testing.

    The constructor is the single definition of the settings: ``from_env()``
    and ``from_file()`` take their names and types from its signature, and
    each parameter is stored as the attribute of the same name in upper case
    (``ws_retry_delay`` -> ``WS_RETRY_DELAY``). A new setting is a new
    parameter plus its attribute; a test checks that the two match.
    """

    def __init__(
        self,
        ws_max_message_size: int = DEFAULT_WS_MESSAGE_SIZE,
        ws_max_queue_size: int = DEFAULT_WS_QUEUE_SIZE,
        http_max_keepalive: int = DEFAULT_KEEPALIVE_CONNS,
        http_max_connections: int = DEFAULT_MAX_CONNS,
        ws_retry_attempts: int = DEFAULT_WS_RETRY_ATTEMPTS,
        ws_retry_delay: float = DEFAULT_WS_RETRY_DELAY,
        port_range_start: int = 8001,
        port_range_end: int = 9000,
    ):
        self.WS_MAX_MESSAGE_SIZE = ws_max_message_size
        self.WS_MAX_QUEUE_SIZE = ws_max_queue_size
        self.HTTP_MAX_KEEPALIVE = http_max_keepalive
        self.HTTP_MAX_CONNECTIONS = http_max_connections
        self.WS_RETRY_ATTEMPTS = ws_retry_attempts
        self.WS_RETRY_DELAY = ws_retry_delay
        self.PORT_RANGE_START = port_range_start
        self.PORT_RANGE_END = port_range_end

    @classmethod
    def from_env(cls, prefix: str = "FASTAPI_TESTING_", *, strict: bool = False) -> Self:
        """Create configuration from environment variables, e.g. ``FASTAPI_TESTING_PORT_RANGE_START``.

        A variable with the prefix that names no setting (a typo) or holds a
        value of the wrong type is ignored and logged as a warning, and the
        default is used. With ``strict=True`` it raises instead, as
        ``from_file()`` does. Every variable starting with ``prefix`` is read as
        a setting, so use a prefix that belongs to this library alone.

        Raises:
            ValueError: with ``strict=True``, unknown variables or invalid values.
        """
        config_params: dict[str, Any] = {}
        problems: list[str] = []
        for env_key, env_value in os.environ.items():
            if not env_key.startswith(prefix):
                continue
            config_key = env_key[len(prefix) :].lower()
            kind = _SETTINGS.get(config_key)
            if kind is None:
                problems.append(f"{env_key}: unknown setting")
                continue
            try:
                config_params[config_key] = kind(env_value)
            except ValueError:
                problems.append(f"{env_key}: must be {_describe(kind)}, got {env_value!r}")

        if problems:
            if strict:
                raise ValueError("Invalid fastapi-testing environment: " + "; ".join(sorted(problems)))
            for problem in sorted(problems):
                logger.warning("Ignoring environment variable %s", problem)
        return cls(**config_params)

    @classmethod
    def from_file(cls, file_path: str | os.PathLike[str]) -> Self:
        """Load configuration from a JSON or TOML file.

        Keys are the ``Config`` parameter names, e.g. ``ws_max_message_size``.
        In a TOML file that has a ``[tool]`` table, such as ``pyproject.toml``,
        settings are read from ``[tool.fastapi-testing]``; if that table is
        absent, the defaults are used.

        Raises:
            ValueError: unsupported file type, unknown keys or wrongly typed values.
        """
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".toml":
            with path.open("rb") as f:
                data = tomllib.load(f)
            if "tool" in data:
                data = data["tool"].get("fastapi-testing", {})
        else:
            raise ValueError(f"Unsupported config file type {path.suffix!r}; use .json or .toml")

        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected an object of settings, got {type(data).__name__}")

        unknown = set(data) - set(_SETTINGS)
        if unknown:
            raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")

        for key, value in data.items():
            kind = _SETTINGS[key]
            # An integer is accepted for a float setting; a bool is not a number here.
            accepted = (int, float) if kind is float else kind
            if isinstance(value, bool) or not isinstance(value, accepted):
                raise ValueError(f"{path}: {key!r} must be {_describe(kind)}, got {value!r}")

        return cls(**data)


# Setting name -> type, from the Config constructor (see Config). The annotations are not
# checked statically; tests/test_config.py::TestSettingsSchema checks they are int or float.
_SETTINGS: dict[str, type[int] | type[float]] = {
    name: parameter.annotation
    for name, parameter in inspect.signature(Config.__init__, eval_str=True).parameters.items()
    if name != "self"
}


def _describe(kind: type) -> str:
    return "an integer" if kind is int else "a number"


# The active configuration. Replace it with set_config(); assigning to an
# imported name (``from fastapi_testing import global_config``) only rebinds
# that local name and is not seen by the library.
global_config = Config()


def get_config() -> Config:
    """Return the active configuration."""
    return global_config


def set_config(config: Config) -> None:
    """Replace the active configuration.

    Takes effect for everything created or connected afterwards: port
    allocation, HTTP client limits, WebSocket defaults and retry behaviour.
    """
    global global_config
    global_config = config


class InvalidResponseTypeError(Exception):
    """Exception raised when an operation is not supported for the response type."""


class ServerStartupError(RuntimeError):
    """Raised when the test server fails to start or does not start in time."""


class ServerShutdownError(RuntimeError):
    """Raised by ``stop()`` when the application's lifespan shutdown fails or times out.

    It is raised after every resource (connections, server, socket, port) has
    been released, so the failing test reports it without leaking anything.
    """


@dataclass
class WebSocketConfig:
    """WebSocket connection configuration.

    Attributes:
        subprotocols: List of supported subprotocols
        compression: Compression algorithm to use
        extra_headers: Additional headers for the connection
        ping_interval: Interval between ping messages
        ping_timeout: Timeout for ping responses
        max_size: Maximum message size in bytes
        max_queue: Maximum number of queued messages
        timeout: Connection timeout in seconds

    A field left as ``None`` uses the library default (``max_size`` and
    ``max_queue`` come from the active ``Config`` at connect time). To pass
    ``None`` through to websockets, e.g. ``ping_interval=None`` to disable
    keepalive pings, use the ``options`` argument of
    ``AsyncTestClient.websocket()``.
    """

    subprotocols: list[str] | None = None
    compression: str | None = None
    extra_headers: dict[str, str] | None = None
    ping_interval: float | None = None
    ping_timeout: float | None = None
    max_size: int | None = None
    max_queue: int | None = None
    timeout: float | None = None


def _new_server_socket() -> socket.socket:
    """A TCP socket configured the way the test server binds its listening socket.

    On POSIX, ``SO_REUSEADDR`` lets a port from a recently closed connection
    (TIME_WAIT) be reused where the OS permits it, and never allows binding a
    port another socket is listening on. On Windows ``SO_REUSEADDR`` would
    allow exactly that, so ``SO_EXCLUSIVEADDRUSE`` is used instead, as asyncio
    does.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "posix":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    return sock


class PortGenerator:
    """Manages port allocation for test servers.

    A range bound left as ``None`` is read from the active ``Config`` each
    time a port is requested.
    """

    def __init__(self, start: int | None = None, end: int | None = None):
        self._start = start
        self._end = end
        self.used_ports: set[int] = set()

    @property
    def start(self) -> int:
        return global_config.PORT_RANGE_START if self._start is None else self._start

    @property
    def end(self) -> int:
        return global_config.PORT_RANGE_END if self._end is None else self._end

    @staticmethod
    def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
        """Whether ``bind()`` could bind ``port`` on ``host`` right now.

        Uses the same socket options as ``bind()``, so the two always agree: a
        port another socket holds is taken, and a port in TIME_WAIT is free
        exactly when the OS lets the server reuse it.
        """
        with _new_server_socket() as sock:
            try:
                sock.bind((host, port))
                return True
            except (OSError, OverflowError):
                return False

    def get_port(self) -> int:
        """Get an available port from the pool.

        The port is only checked, not held: another process can take it before
        it is bound. ``AsyncTestServer`` uses ``bind()`` instead.
        """
        available_ports = set(range(self.start, self.end + 1)) - self.used_ports
        if not available_ports:
            raise RuntimeError(f"No available ports in range {self.start}-{self.end}")

        while available_ports:
            port = random.choice(list(available_ports))
            if self.is_port_available(port):
                self.used_ports.add(port)
                return port
            available_ports.remove(port)
        raise RuntimeError(f"No available ports found in range {self.start}-{self.end}")

    def release_port(self, port: int) -> None:
        """Release a port back to the pool."""
        self.used_ports.discard(port)

    def bind(self, host: str) -> socket.socket:
        """Bind a TCP socket on ``host`` to a free port in the range and reserve it.

        The socket is bound and listening when returned: binding is the
        availability check, and listening right away makes the port exclusive
        (on Linux, two ``SO_REUSEADDR`` sockets may bind the same port until one
        listens), so unlike ``get_port()`` followed by a later bind, no other
        process can take it. Connections that arrive before the server starts
        serving wait in the backlog. The caller owns the socket and must close
        it and call ``release_port()``.
        """
        candidates = list(set(range(self.start, self.end + 1)) - self.used_ports)
        if not candidates:
            raise RuntimeError(f"No available ports in range {self.start}-{self.end}")
        random.shuffle(candidates)
        for port in candidates:
            sock = _new_server_socket()
            try:
                sock.bind((host, port))
                sock.listen()
            except (OSError, OverflowError):
                sock.close()
                continue
            self.used_ports.add(port)
            return sock
        raise RuntimeError(f"No available ports found in range {self.start}-{self.end}")


class AsyncTestResponse:
    """Enhanced response wrapper supporting both HTTP and WebSocket responses.

    Provides unified interface for handling both HTTP and WebSocket responses
    with proper type checking and error handling.
    """

    def __init__(self, response: httpx.Response | ClientConnection):
        self._response = response

    def _http(self, message: str) -> httpx.Response:
        """The wrapped HTTP response; raises InvalidResponseTypeError with ``message`` for a WebSocket."""
        if isinstance(self._response, ClientConnection):
            raise InvalidResponseTypeError(message)
        return self._response

    async def json(self) -> Any:
        """Get JSON response (HTTP only)."""
        return self._http("Cannot get JSON directly from WebSocket response. Use websocket() methods instead.").json()

    async def text(self) -> str:
        """Get text response (HTTP only)."""
        return self._http("Cannot get text directly from WebSocket response. Use websocket() methods instead.").text

    @property
    def status_code(self) -> int:
        """Get status code (HTTP only)."""
        return self._http("WebSocket connections don't have status codes").status_code

    @property
    def headers(self) -> httpx.Headers:
        """Get response headers (HTTP only)."""
        return self._http("WebSocket connections don't have headers").headers

    @property
    def httpx_response(self) -> httpx.Response:
        """The underlying ``httpx.Response`` (HTTP only), for anything this wrapper does not cover."""
        return self._http("This response is a WebSocket connection; use websocket() instead")

    def websocket(self) -> ClientConnection:
        """Get WebSocket connection (WebSocket only)."""
        if not isinstance(self._response, ClientConnection):
            raise InvalidResponseTypeError("This response is not a WebSocket connection")
        return self._response

    async def expect_status(self, status_code: int) -> Self:
        """Assert expected status code (HTTP only)."""
        response = self._http("WebSocket connections don't have status codes")
        if response.status_code != status_code:
            # An explicit raise, not `assert`, so the check also runs under `python -O`.
            body = response.text
            if len(body) > 500:
                body = body[:500] + "..."
            raise AssertionError(f"Expected status {status_code}, got {response.status_code}: {body}")
        return self


class WebSocketHelper:
    """Helper methods for WebSocket operations."""

    @staticmethod
    async def send_json(resp: AsyncTestResponse, data: Any) -> None:
        """Send JSON data over WebSocket."""
        ws = resp.websocket()
        await ws.send(json.dumps(data))

    @staticmethod
    async def receive_json(resp: AsyncTestResponse) -> Any:
        """Receive JSON data from WebSocket."""
        ws = resp.websocket()
        data = await ws.recv()
        if not isinstance(data, str):
            raise TypeError(f"Expected text data to decode JSON, got {type(data)}")
        return json.loads(data)

    @staticmethod
    async def send_binary(resp: AsyncTestResponse, data: bytes) -> None:
        """Send binary data over WebSocket."""
        ws = resp.websocket()
        await ws.send(data)

    @staticmethod
    async def receive_binary(resp: AsyncTestResponse) -> bytes:
        """Receive binary data from WebSocket."""
        ws = resp.websocket()
        data = await ws.recv()
        if not isinstance(data, bytes):
            raise TypeError(f"Expected bytes, got {type(data)}")
        return data

    @staticmethod
    async def send_text(resp: AsyncTestResponse, data: str) -> None:
        """Send text data over WebSocket."""
        ws = resp.websocket()
        await ws.send(data)

    @staticmethod
    async def receive_text(resp: AsyncTestResponse) -> str:
        """Receive text data from WebSocket."""
        ws = resp.websocket()
        data = await ws.recv()
        if not isinstance(data, str):
            raise TypeError(f"Expected str, got {type(data)}")
        return data

    @staticmethod
    async def expect_message(
        resp: AsyncTestResponse, expected: str | dict[str, Any] | bytes, timeout: float | None = None
    ) -> None:
        """Assert expected message is received within timeout."""
        ws = resp.websocket()
        try:
            message = await asyncio.wait_for(ws.recv(), timeout)
        except TimeoutError as e:
            raise TimeoutError(f"No WebSocket message received within {timeout} s") from e

        if isinstance(expected, dict):
            if not isinstance(message, str):
                raise AssertionError(f"Expected a text message for JSON decoding, got {type(message)}")
            if json.loads(message) != expected:
                raise AssertionError(f"Expected message {expected}, got {message}")
        else:
            if message != expected:
                raise AssertionError(f"Expected message {expected}, got {message}")

    @staticmethod
    async def drain_messages(resp: AsyncTestResponse, timeout: float = 0.1) -> list[Any]:
        """Receive messages until none arrives within ``timeout`` seconds or the connection closes normally.

        Returns the messages received. A normal close by the server ends the
        drain without losing what was already received. An abnormal close (for
        example 1011 when the server's handler raised) propagates as
        ``ConnectionClosedError``: the server failed, and the test should see it.
        """
        if timeout is None:
            raise ValueError("drain_messages() needs a timeout; without one it would wait forever")
        ws = resp.websocket()
        messages: list[Any] = []
        try:
            while True:
                messages.append(await asyncio.wait_for(ws.recv(), timeout))
        except (TimeoutError, ConnectionClosedOK):
            pass
        return messages


class AsyncTestClient:
    """Async test client supporting both HTTP and WebSocket connections.

    ``async with client:`` closes the client on exit when ``close_on_exit`` is
    true (the default for a client you create). The client an
    ``AsyncTestServer`` provides is created with ``close_on_exit=False``: it is
    shared for the server's lifetime and closed by ``stop()``, so
    ``async with server.client as client:`` can be used any number of times.
    """

    def __init__(
        self, base_url: str, timeout: float = 30.0, follow_redirects: bool = True, *, close_on_exit: bool = True
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._close_on_exit = close_on_exit
        self._websocket_connections: set[ClientConnection] = set()

        limits = httpx.Limits(
            max_keepalive_connections=global_config.HTTP_MAX_KEEPALIVE,
            max_connections=global_config.HTTP_MAX_CONNECTIONS,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, follow_redirects=follow_redirects, limits=limits
        )

        self.ws = WebSocketHelper()

    async def close(self) -> None:
        """Close all connections and cleanup resources."""
        # Clean up any active websocket connections
        for ws in list(self._websocket_connections):
            try:
                if ws.state == State.CLOSED:
                    self._websocket_connections.discard(ws)
                else:
                    await ws.close()
                    self._websocket_connections.discard(ws)
            except Exception as e:
                logger.warning("Error closing websocket connection: %s", e)
        self._websocket_connections.clear()

        if self._client:
            await self._client.aclose()

    async def request(self, method: str, url: str, **kwargs: Any) -> AsyncTestResponse:
        """Make HTTP request."""
        response = await self._client.request(method, url, **kwargs)
        return AsyncTestResponse(response)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncGenerator[httpx.Response]:
        """Make a streaming HTTP request, e.g. for Server-Sent Events.

        Yields the ``httpx.Response`` before its body is read; iterate it with
        ``aiter_lines()``, ``aiter_text()`` or ``aiter_bytes()``. The response is
        closed when the block exits::

            async with server.client.stream("GET", "/events") as response:
                async for line in response.aiter_lines():
                    ...
        """
        async with self._client.stream(method, url, **kwargs) as response:
            yield response

    def _websocket_url(self, path: str) -> str:
        """The ws:// or wss:// URL for ``path`` on this client's base URL."""
        scheme, separator, rest = self._base_url.partition("://")
        if not separator or scheme not in ("http", "https"):
            raise ValueError("Invalid base URL. Must start with 'http://' or 'https://'")
        return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{path}"

    async def websocket(
        self, path: str, config: WebSocketConfig | None = None, options: dict[str, Any] | None = None
    ) -> AsyncTestResponse:
        """Create a websocket connection with configuration.

        Connection errors (refused, closed before the handshake answer, timed
        out) are retried ``WS_RETRY_ATTEMPTS``
        times; a rejected handshake (for example 403 or 404) fails at once.
        """
        ws_url = self._websocket_url(path)

        connect_kwargs: dict[str, Any] = {
            "open_timeout": self._timeout,
            "max_size": global_config.WS_MAX_MESSAGE_SIZE,
            "max_queue": global_config.WS_MAX_QUEUE_SIZE,
        }

        if config:
            # None means "not set"; any other value, including 0, is passed through.
            settings = {
                "subprotocols": config.subprotocols,
                "compression": config.compression,
                "additional_headers": config.extra_headers,
                "ping_interval": config.ping_interval,
                "ping_timeout": config.ping_timeout,
                "max_size": config.max_size,
                "max_queue": config.max_queue,
                "open_timeout": config.timeout,
            }
            connect_kwargs.update({key: value for key, value in settings.items() if value is not None})

        if options:
            connect_kwargs.update(options)

        # Retry only what can be transient: the server not accepting yet, closing
        # the connection before answering (InvalidMessage), or a timeout. A
        # rejected handshake (InvalidStatus) or a bad URI is final.
        attempts = max(global_config.WS_RETRY_ATTEMPTS, 1)
        attempt = 1
        while True:
            try:
                ws = await connect(ws_url, **connect_kwargs)
                break
            except (OSError, TimeoutError, InvalidMessage) as e:
                logger.debug("WebSocket connection attempt %d/%d to %s failed: %s", attempt, attempts, ws_url, e)
                if attempt >= attempts:
                    raise
            attempt += 1
            await asyncio.sleep(global_config.WS_RETRY_DELAY)

        self._websocket_connections.add(ws)
        return AsyncTestResponse(ws)

    async def get(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request("DELETE", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request("PATCH", url, **kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._close_on_exit:
            await self.close()


# The signals uvicorn handles while serving; Ctrl+Break on Windows.
_HANDLED_SIGNALS: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM) + (
    (signal.SIGBREAK,) if sys.platform == "win32" else ()
)
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None


class _SignalHandlers:
    """Process-wide SIGINT/SIGTERM handling for every running test server.

    uvicorn's ``capture_signals()`` saves the handler installed when ``serve()``
    starts and restores it when ``serve()`` ends. That is only correct when
    servers stop in reverse order of starting: stopping overlapping servers
    A then B left A's dead ``handle_exit`` installed, so Ctrl-C and SIGTERM were
    ignored for the rest of the process.

    Here the original handlers are saved when the first server starts and
    restored when the last one stops. In between, the installed handler is
    always the ``handle_exit`` of the most recently started server that is
    still running, the same bound method uvicorn installs (sse-starlette finds
    the server through it).
    """

    def __init__(self) -> None:
        self._running: list[uvicorn.Server] = []
        self._originals: dict[int, _SignalHandler] = {}

    def enter(self, server: uvicorn.Server) -> None:
        if not self._running:
            self._originals = {sig: signal.getsignal(sig) for sig in _HANDLED_SIGNALS}
        self._running.append(server)
        self._install()

    def leave(self, server: uvicorn.Server) -> None:
        with suppress(ValueError):
            self._running.remove(server)
        if self._running:
            self._install()
        else:
            originals, self._originals = self._originals, {}
            for sig, handler in originals.items():
                signal.signal(sig, handler)

    def _install(self) -> None:
        handler = self._running[-1].handle_exit
        for sig in _HANDLED_SIGNALS:
            signal.signal(sig, handler)


_signal_handlers = _SignalHandlers()


class UvicornTestServer(uvicorn.Server):
    """Uvicorn test server with startup event support."""

    def __init__(self, config: uvicorn.Config, startup_handler: asyncio.Event):
        super().__init__(config)
        self.startup_handler = startup_handler

    @override
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Override startup to signal when ready.

        Readiness is not signalled when uvicorn's own startup gave up and is
        exiting; ``AsyncTestServer.start()`` then sees the server task end.
        """
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self.startup_handler.set()

    @override
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        """Capture SIGINT/SIGTERM like uvicorn, but correctly for overlapping servers.

        See ``_SignalHandlers``. As in uvicorn, signals can only be handled in
        the main thread, and a signal received while serving is raised again
        once this server has stopped, so it reaches the next running server or
        finally the original handler (Ctrl-C then raises ``KeyboardInterrupt``).
        """
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        _signal_handlers.enter(self)
        try:
            yield
        finally:
            _signal_handlers.leave(self)
        for captured_signal in reversed(self._captured_signals):
            signal.raise_signal(captured_signal)


class _LifespanRunner:
    """Runs an ASGI application's lifespan protocol in a task this library owns.

    uvicorn runs the lifespan in a task it keeps no reference to, exits the
    process on startup failure and only logs shutdown failures. Driving the
    protocol here instead gives ``AsyncTestServer`` control over it: a hung
    startup or shutdown can be timed out and cancelled, and failures surface
    as exceptions chained to the application's own error. The whole
    application is called, so middleware sees the lifespan scope as usual.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.state: dict[str, Any] = {}
        self._messages: asyncio.Queue[Message] = asyncio.Queue()
        self._startup_done = asyncio.Event()
        self._shutdown_done = asyncio.Event()
        self._failure: str | None = None
        self.startup_completed = False
        self._receive_calls = 0
        self.unsupported = False
        self.task: asyncio.Task[None] | None = None

    async def _receive(self) -> Message:
        self._receive_calls += 1
        return await self._messages.get()

    async def _send(self, message: Message) -> None:
        kind = message["type"]
        if kind in ("lifespan.startup.failed", "lifespan.shutdown.failed"):
            self._failure = message.get("message") or kind
        elif kind == "lifespan.startup.complete":
            self.startup_completed = True
        if kind.startswith("lifespan.startup."):
            self._startup_done.set()
        elif kind.startswith("lifespan.shutdown."):
            self._shutdown_done.set()

    async def _main(self) -> None:
        scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": self.state}
        try:
            await self.app(scope, self._receive, self._send)
            if self._receive_calls == 0:
                # Returned without reading the startup message: the application
                # ignores lifespan, which uvicorn accepts.
                self.unsupported = True
        except Exception:
            if self._receive_calls == 0:
                # Raised before reading the startup message: the application does
                # not implement the lifespan protocol (uvicorn's "auto" mode).
                self.unsupported = True
                logger.debug("Application does not support lifespan; continuing without it", exc_info=True)
                return
            raise
        finally:
            self._startup_done.set()
            self._shutdown_done.set()

    async def _wait(self, done: asyncio.Event, timeout: float, phase: str) -> None:
        assert self.task is not None
        waiter = asyncio.ensure_future(done.wait())
        try:
            finished, _ = await asyncio.wait({waiter, self.task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if not finished:
            await self.cancel()
            raise TimeoutError(f"Application {phase} did not complete within {timeout}s")
        if self._failure is not None or (self.task.done() and not self.unsupported):
            # Give the application a moment to raise after reporting failure, so
            # the original exception can be chained.
            await asyncio.wait({self.task}, timeout=1.0)
            error = self.task.exception() if self.task.done() and not self.task.cancelled() else None
            if self._failure is None and error is None:
                # The application returned without reporting failure: uvicorn accepts
                # this in both phases (its lifespan task ends and serving goes on).
                if phase == "startup" and not self.startup_completed:
                    logger.debug("Application returned during lifespan startup without completing it")
                return
            # An application that reported failure but kept running is not left behind.
            await self.cancel()
            # Prefer the exception itself: the failure message from Starlette is a full traceback.
            raise RuntimeError(
                f"Application {phase} failed: {error!r}" if error else f"Application {phase} failed: {self._failure}"
            ) from error

    async def startup(self, timeout: float) -> None:
        self.task = asyncio.create_task(self._main(), name="fastapi-testing lifespan")
        await self._messages.put({"type": "lifespan.startup"})
        await self._wait(self._startup_done, timeout, "startup")

    async def shutdown(self, timeout: float) -> None:
        if self.task is None or self.task.done():
            return
        self._failure = None  # Each phase reports its own failure.
        await self._messages.put({"type": "lifespan.shutdown"})
        await self._wait(self._shutdown_done, timeout, "shutdown")
        # The application is expected to return after shutdown.complete.
        if not self.task.done():
            await asyncio.wait({self.task}, timeout=1.0)
            await self.cancel()

    # How long cancel() waits for the lifespan task. The bound keeps
    # startup_timeout and shutdown_timeout meaningful when the application
    # suppresses cancellation and keeps awaiting.
    cancel_timeout: float = 5.0

    async def cancel(self) -> None:
        """Cancel the lifespan task and wait for it, at most ``cancel_timeout`` seconds."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            done, _ = await asyncio.wait({self.task}, timeout=self.cancel_timeout)
            if not done:
                logger.warning("Application lifespan ignored cancellation for %ss; leaving it", self.cancel_timeout)


# Use the configurable PortGenerator instance
_port_generator = PortGenerator()


class AsyncTestServer:
    """Async test server with proper lifecycle management and WebSocket support.

    The server owns its whole lifecycle; uvicorn only serves connections:

    * The listening socket is bound by this class (no port race) and always
      closed by ``stop()``.
    * The application's lifespan runs in a task owned by this class: startup
      failures raise ``ServerStartupError`` chained to the application's own
      exception, a hung startup fails within ``startup_timeout``, and shutdown
      failures raise ``ServerShutdownError``.
    * ``stop()`` runs in a fixed order: close the client, stop HTTP (open
      connections get ``shutdown_timeout`` to finish, then are closed), run the
      lifespan shutdown, close the socket, release the port. Every step runs
      even when an earlier one fails or ``stop()`` is cancelled.

    Args:
        lifespan: lifespan for the empty ``FastAPI`` app the server creates.
        startup_timeout: seconds the application's lifespan startup and the
            server's own startup may take together.
        shutdown_timeout: seconds open connections get to finish, and then the
            lifespan shutdown gets, when the server stops.
        app: the application to serve, e.g. ``create_app()``. Its own lifespan
            runs; combining it with ``lifespan`` is an error. Without it the
            server creates an empty ``FastAPI`` app to add routes to.
        client_timeout: timeout in seconds for each request made with
            ``server.client`` and for opening its WebSocket connections.

    ``app`` may also be replaced (``server.app = create_app()``) before ``start()``.
    """

    def __init__(
        self,
        lifespan: Lifespan[FastAPI] | None = None,
        startup_timeout: float = 30.0,
        shutdown_timeout: float = 10.0,
        *,
        app: FastAPI | None = None,
        client_timeout: float = 30.0,
    ):
        if app is not None and lifespan is not None:
            raise ValueError("Pass either app or lifespan: an application brings its own lifespan")
        self.app = app if app is not None else FastAPI(lifespan=lifespan)
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.client_timeout = client_timeout
        self._server_task: asyncio.Task[None] | None = None
        self._port: int | None = None
        self._host = "127.0.0.1"
        self._client: AsyncTestClient | None = None
        self._server: UvicornTestServer | None = None
        self._socket: socket.socket | None = None
        self._lifespan: _LifespanRunner | None = None

    async def _asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The ASGI entry point given to uvicorn.

        uvicorn runs with ``lifespan="off"``, so it would hand every request an
        empty ``state``; copy in the state the lifespan yielded instead, as
        uvicorn does when it runs the lifespan itself.
        """
        if self._lifespan is not None and scope["type"] in ("http", "websocket"):
            scope["state"] = {**self._lifespan.state, **scope.get("state", {})}
        await self.app(scope, receive, send)

    async def _serve(self) -> None:
        """Run uvicorn, turning its ``sys.exit()`` on startup failure into an exception.

        With the socket and lifespan owned here, uvicorn has little left that can
        fail at startup, but a ``SystemExit`` escaping an asyncio task would stop
        the whole event loop, so it is still converted and reported by ``start()``.
        """
        assert self._server is not None and self._socket is not None
        try:
            await self._server.serve(sockets=[self._socket])
        except SystemExit as e:
            raise ServerStartupError(
                f"Server failed to start on host {self._host} and port {self._port} (uvicorn exit code {e.code})"
            ) from e

    async def start(self) -> None:
        """Start the server: bind the socket, run lifespan startup, then serve.

        Raises:
            ServerStartupError: no free port, the application's lifespan startup
                failed or timed out, or uvicorn failed to start. Everything
                acquired so far is released first.
        """
        if self._server_task is not None or self._lifespan is not None:
            raise RuntimeError("Server is already running")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout

        def remaining() -> float:
            return max(deadline - loop.time(), 0.0)

        try:
            try:
                self._socket = _port_generator.bind(self._host)
            except RuntimeError as e:
                raise ServerStartupError(str(e)) from e
            port: int = self._socket.getsockname()[1]
            self._port = port

            self._lifespan = _LifespanRunner(self.app)
            try:
                await self._lifespan.startup(timeout=remaining())
            except TimeoutError as e:
                raise ServerStartupError(
                    f"Server startup timed out on host {self._host} and port {self._port}: {e}"
                ) from e
            except RuntimeError as e:
                raise ServerStartupError(f"{e} (host {self._host}, port {self._port})") from (e.__cause__ or e)

            startup_handler = asyncio.Event()
            config = uvicorn.Config(
                app=self._asgi,
                host=self._host,
                port=port,
                log_level="error",
                lifespan="off",
                interface="asgi3",
                # uvicorn types this as whole seconds.
                timeout_graceful_shutdown=math.ceil(self.shutdown_timeout),
            )
            self._server = UvicornTestServer(config=config, startup_handler=startup_handler)
            self._server_task = asyncio.create_task(self._serve(), name="fastapi-testing uvicorn")
            started = asyncio.ensure_future(startup_handler.wait())
            try:
                done, _ = await asyncio.wait(
                    {self._server_task, started}, timeout=remaining(), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                started.cancel()
            if started not in done:
                if self._server_task in done:
                    task = self._server_task
                    error = None if task.cancelled() else task.exception()
                    if isinstance(error, ServerStartupError):
                        raise error
                    raise ServerStartupError(
                        f"Server exited during startup on host {self._host} and port {self._port}"
                    ) from error
                raise ServerStartupError(f"Server startup timed out on host {self._host} and port {self._port}")

            self._client = AsyncTestClient(base_url=self.base_url, timeout=self.client_timeout, close_on_exit=False)
        except BaseException:
            with suppress(Exception):
                # stop() releases what was acquired; a lifespan shutdown error is
                # secondary to the startup failure being reported.
                await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the server and release every resource, in a fixed order.

        1. Close the client and its WebSocket connections.
        2. Stop HTTP: stop accepting, give open connections ``shutdown_timeout``
           to finish, then close them (uvicorn's graceful shutdown).
        3. Run the application's lifespan shutdown, within ``shutdown_timeout``.
        4. Close the socket and release the port.

        Safe to call more than once and after a failed ``start()``. If a step
        fails or ``stop()`` is cancelled, the remaining steps still run (without
        waiting) before the error or cancellation propagates.

        Raises:
            ServerShutdownError: the lifespan shutdown failed or timed out.
        """
        client, self._client = self._client, None
        server, self._server = self._server, None
        task, self._server_task = self._server_task, None
        lifespan, self._lifespan = self._lifespan, None
        sock, self._socket = self._socket, None
        port, self._port = self._port, None

        shutdown_error: BaseException | None = None
        graceful = False
        try:
            if client is not None:
                await client.close()

            if task is not None:
                if server is not None:
                    server.should_exit = True
                # uvicorn bounds the connection wait by timeout_graceful_shutdown;
                # the margin covers its own polling and closing.
                await asyncio.wait({task}, timeout=self.shutdown_timeout + 5.0)
                if not task.done() and server is not None:
                    logger.error("Forcing server exit on host %s port %s", self._host, port)
                    server.force_exit = True
                    await asyncio.wait({task}, timeout=2.0)

            if lifespan is not None:
                if lifespan.startup_completed:
                    try:
                        await lifespan.shutdown(timeout=self.shutdown_timeout)
                    except (TimeoutError, RuntimeError) as e:
                        shutdown_error = e
                else:
                    # Startup never completed (start() failed or was cancelled while it
                    # ran), so there is nothing to shut down: an application still in
                    # startup would never read the shutdown message.
                    await lifespan.cancel()
            graceful = True
        finally:
            # Runs also when a step above failed or stop() was cancelled.
            try:
                if task is not None and not task.done():
                    task.cancel()
                    if graceful:
                        await asyncio.wait({task})
                if lifespan is not None and not graceful and lifespan.task is not None:
                    # Interrupted: cancel the lifespan rather than wait for it.
                    lifespan.task.cancel()
                if task is not None and task.done() and not task.cancelled() and task.exception() is not None:
                    # A failed startup is reported by start(); anything else is not actionable here.
                    logger.debug("Server task ended with an error", exc_info=task.exception())
            finally:
                # When uvicorn did not run its own shutdown (interrupted stop(), or a
                # start() that ended during uvicorn's startup), its asyncio servers
                # still watch the socket. Closing them first removes that
                # registration, so the closed file descriptor is not left in the
                # event loop's selector for a later socket to collide with.
                for listener in getattr(server, "servers", ()):
                    listener.close()
                if sock is not None:
                    sock.close()
                if port is not None:
                    _port_generator.release_port(port)

        if shutdown_error is not None:
            raise ServerShutdownError(f"{shutdown_error} (host {self._host}, port {port})") from (
                shutdown_error.__cause__ or shutdown_error
            )

    @property
    def base_url(self) -> str:
        if not self._port:
            raise RuntimeError("Server is not running")
        return f"http://{self._host}:{self._port}"

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self._stop_after(exc_val)

    async def _stop_after(self, error: BaseException | None) -> None:
        """Stop the server once the block using it has ended.

        When the block raised, its exception is the failure to report: a
        ``ServerShutdownError`` from ``stop()`` is attached to it as a note
        instead of replacing it. When the block succeeded, the shutdown error
        propagates as usual.
        """
        if error is None:
            await self.stop()
            return
        try:
            await self.stop()
        except ServerShutdownError as e:
            cause = e.__cause__
            # The message usually names the cause already; add it only when it does not.
            detail = f" (caused by {type(cause).__name__}: {cause})" if cause and str(cause) not in str(e) else ""
            error.add_note(f"fastapi-testing: stopping the server also failed: {e}{detail}")
            logger.warning("Stopping the server failed after the test failed: %s", e, exc_info=e)

    @property
    def client(self) -> AsyncTestClient:
        if not self._client:
            raise RuntimeError("Server is not running")
        return self._client


@asynccontextmanager
async def create_test_server(
    lifespan: Lifespan[FastAPI] | None = None,
    startup_timeout: float = 30.0,
    shutdown_timeout: float = 10.0,
    *,
    app: FastAPI | None = None,
    client_timeout: float = 30.0,
) -> AsyncGenerator[AsyncTestServer, None]:
    """Start an ``AsyncTestServer``, yield it, and stop it when the block exits.

    The arguments are those of ``AsyncTestServer``. When the block raises, its
    exception is what propagates; a shutdown error is attached to it as a note.
    """
    server = AsyncTestServer(
        lifespan=lifespan,
        startup_timeout=startup_timeout,
        shutdown_timeout=shutdown_timeout,
        app=app,
        client_timeout=client_timeout,
    )
    await server.start()  # releases everything itself when it fails
    try:
        yield server
    except BaseException as e:
        await server._stop_after(e)
        raise
    await server.stop()
