import asyncio
import json
import logging
import os
import tomllib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.applications import AppType
from starlette.types import Lifespan
from websockets.asyncio.client import ClientConnection, connect
from websockets.protocol import State

logger = logging.getLogger(__name__)

# Configuration Constants
DEFAULT_WS_MESSAGE_SIZE = 2**20  # 1MB
DEFAULT_WS_QUEUE_SIZE = 32
DEFAULT_KEEPALIVE_CONNS = 20
DEFAULT_MAX_CONNS = 100
DEFAULT_WS_RETRY_ATTEMPTS = 3
DEFAULT_WS_RETRY_DELAY = 1.0


class Config:
    """
    Global configuration settings for fastapi-testing framework.
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
    def from_env(cls, prefix: str = "FASTAPI_TESTING_") -> "Config":
        """
        Create configuration from environment variables.
        This is an explicit opt-in method.
        """
        # Only look for variables with the specified prefix
        env_vars = {k: v for k, v in os.environ.items() if k.startswith(prefix)}

        # Convert to expected parameter names
        config_params: dict[str, Any] = {}
        for env_key, env_value in env_vars.items():
            config_key = env_key[len(prefix) :].lower()
            # Handle type conversion
            if config_key in _INT_SETTINGS:
                with suppress(ValueError):
                    config_params[config_key] = int(env_value)
            elif config_key in _FLOAT_SETTINGS:
                with suppress(ValueError):
                    config_params[config_key] = float(env_value)

        return cls(**config_params)

    @classmethod
    def from_file(cls, file_path: str | os.PathLike[str]) -> "Config":
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

        unknown = set(data) - _INT_SETTINGS - _FLOAT_SETTINGS
        if unknown:
            raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")

        for key, value in data.items():
            expected = int if key in _INT_SETTINGS else (int, float)
            if isinstance(value, bool) or not isinstance(value, expected):
                kind = "an integer" if key in _INT_SETTINGS else "a number"
                raise ValueError(f"{path}: {key!r} must be {kind}, got {value!r}")

        return cls(**data)


_INT_SETTINGS = frozenset(
    {
        "ws_max_message_size",
        "ws_max_queue_size",
        "http_max_keepalive",
        "http_max_connections",
        "ws_retry_attempts",
        "port_range_start",
        "port_range_end",
    }
)
_FLOAT_SETTINGS = frozenset({"ws_retry_delay"})

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
    def is_port_available(port: int) -> bool:
        import socket
        from contextlib import closing

        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            try:
                sock.bind(("localhost", port))
                return True
            except (OSError, OverflowError):
                return False

    def get_port(self) -> int:
        """Get an available port from the pool."""
        available_ports = set(range(self.start, self.end + 1)) - self.used_ports
        if not available_ports:
            raise RuntimeError(f"No available ports in range {self.start}-{self.end}")

        import random

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


class AsyncTestResponse:
    """Enhanced response wrapper supporting both HTTP and WebSocket responses.

    Provides unified interface for handling both HTTP and WebSocket responses
    with proper type checking and error handling.
    """

    def __init__(self, response: httpx.Response | ClientConnection):
        self._response = response
        self._is_websocket = isinstance(response, ClientConnection)

    async def json(self) -> Any:
        """Get JSON response (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError(
                "Cannot get JSON directly from WebSocket response. Use websocket() methods instead."
            )
        return self._response.json()

    async def text(self) -> str:
        """Get text response (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError(
                "Cannot get text directly from WebSocket response. Use websocket() methods instead."
            )
        return self._response.text

    @property
    def status_code(self) -> int:
        """Get status code (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError("WebSocket connections don't have status codes")
        return self._response.status_code

    @property
    def headers(self) -> httpx.Headers:
        """Get response headers (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError("WebSocket connections don't have headers")
        return self._response.headers

    def websocket(self) -> ClientConnection:
        """Get WebSocket connection (WebSocket only)."""
        if not self._is_websocket:
            raise InvalidResponseTypeError("This response is not a WebSocket connection")
        return self._response

    async def expect_status(self, status_code: int) -> "AsyncTestResponse":
        """Assert expected status code (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError("WebSocket connections don't have status codes")
        assert self._response.status_code == status_code, (
            f"Expected status {status_code}, got {self._response.status_code}"
        )
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
        resp: AsyncTestResponse, expected: str | dict | bytes, timeout: float | None = None
    ) -> None:
        """Assert expected message is received within timeout."""
        ws = resp.websocket()
        try:
            message = await asyncio.wait_for(ws.recv(), timeout)
        except TimeoutError as e:
            logger.error("Timed out waiting for message")
            raise e

        if isinstance(expected, dict):
            if not isinstance(message, str):
                raise AssertionError(f"Expected a text message for JSON decoding, got {type(message)}")
            if json.loads(message) != expected:
                raise AssertionError(f"Expected message {expected}, got {message}")
        else:
            if message != expected:
                raise AssertionError(f"Expected message {expected}, got {message}")

    @staticmethod
    async def drain_messages(resp: AsyncTestResponse, timeout: float | None = 0.1) -> list[Any]:
        """Drain all pending messages from websocket queue."""
        ws = resp.websocket()
        messages = []
        try:
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout)
                messages.append(message)
        except TimeoutError:
            pass
        return messages


class AsyncTestClient:
    """Async test client supporting both HTTP and WebSocket connections."""

    def __init__(self, base_url: str, timeout: float = 30.0, follow_redirects: bool = True):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._websocket_connections: set[ClientConnection] = set()

        limits = httpx.Limits(
            max_keepalive_connections=global_config.HTTP_MAX_KEEPALIVE,
            max_connections=global_config.HTTP_MAX_CONNECTIONS,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, follow_redirects=follow_redirects, limits=limits, http2=True
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
                logger.warning(f"Error closing websocket connection: {e}")
        self._websocket_connections.clear()

        if self._client:
            await self._client.aclose()

    async def request(self, method: str, url: str, **kwargs: Any) -> AsyncTestResponse:
        """Make HTTP request."""
        response = await self._client.request(method, url, **kwargs)
        return AsyncTestResponse(response)

    async def websocket(
        self, path: str, config: WebSocketConfig | None = None, options: dict[str, Any] | None = None
    ) -> AsyncTestResponse:
        """Create a websocket connection with configuration."""
        if not (self._base_url.startswith("http://") or self._base_url.startswith("https://")):
            raise ValueError("Invalid base URL. Must start with 'http://' or 'https://'")
        if self._base_url.startswith("https://"):
            ws_url = f"wss://{self._base_url.replace('https://', '')}{path}"
        elif self._base_url.startswith("http://"):
            ws_url = f"ws://{self._base_url.replace('http://', '')}{path}"
        else:
            ws_url = f"ws://{self._base_url}{path}"

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

        # Retry logic for establishing a WebSocket connection.
        attempt = 0
        while True:
            try:
                ws = await connect(ws_url, **connect_kwargs)
                break
            except Exception as e:
                attempt += 1
                if attempt >= global_config.WS_RETRY_ATTEMPTS:
                    logger.error(
                        f"Failed to establish WebSocket connection after "
                        f"{global_config.WS_RETRY_ATTEMPTS} attempts: {e}"
                    )
                    raise
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

    async def __aenter__(self) -> "AsyncTestClient":
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()


class UvicornTestServer(uvicorn.Server):
    """Uvicorn test server with startup event support."""

    def __init__(self, config: uvicorn.Config, startup_handler: asyncio.Event):
        super().__init__(config)
        self.startup_handler = startup_handler

    async def startup(self, sockets: list | None = None) -> None:
        """Override startup to signal when ready.

        Readiness is not signalled when uvicorn's own startup gave up and is
        exiting; ``AsyncTestServer.start()`` then sees the server task end.
        """
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self.startup_handler.set()


# Use the configurable PortGenerator instance
_port_generator = PortGenerator()


class AsyncTestServer:
    """Async test server with proper lifecycle management and WebSocket support."""

    def __init__(
        self,
        lifespan: Lifespan[AppType] | None = None,
        startup_timeout: float = 30.0,
        shutdown_timeout: float = 10.0,
    ):
        self.app = FastAPI(lifespan=lifespan)
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._server_task: asyncio.Task | None = None
        self._port: int | None = None
        self._host = "127.0.0.1"
        self._client: AsyncTestClient | None = None
        self._server: UvicornTestServer | None = None

    async def _serve(self) -> None:
        """Run uvicorn, turning its ``sys.exit()`` on startup failure into an exception.

        uvicorn exits the process when binding or lifespan startup fails. A
        ``SystemExit`` escaping an asyncio task stops the whole event loop, so
        it is converted here and reported by ``start()`` instead.
        """
        assert self._server is not None
        try:
            await self._server.serve()
        except SystemExit as e:
            raise ServerStartupError(
                f"Server failed to start on host {self._host} and port {self._port} (uvicorn exit code {e.code})"
            ) from e

    async def start(self) -> None:
        """Start the server asynchronously with proper lifecycle management.

        Raises:
            ServerStartupError: uvicorn failed to start, or did not start in time.
        """
        if self._server_task is not None:
            raise RuntimeError("Server is already running")

        self._port = _port_generator.get_port()
        startup_handler = asyncio.Event()

        config = uvicorn.Config(app=self.app, host=self._host, port=self._port, log_level="error", loop="asyncio")

        self._server = UvicornTestServer(config=config, startup_handler=startup_handler)

        self._server_task = asyncio.create_task(self._serve())
        started = asyncio.create_task(startup_handler.wait())

        try:
            done, _ = await asyncio.wait(
                {self._server_task, started}, timeout=self.startup_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if started not in done:
                if self._server_task in done:
                    error = self._server_task.exception()
                    if isinstance(error, ServerStartupError):
                        raise error
                    raise ServerStartupError(
                        f"Server exited during startup on host {self._host} and port {self._port}"
                    ) from error
                raise ServerStartupError(f"Server startup timed out on host {self._host} and port {self._port}")

            self._client = AsyncTestClient(base_url=self.base_url, timeout=self.startup_timeout)
        except BaseException:
            started.cancel()
            # Startup never completed, so uvicorn cannot shut down gracefully
            # (it does not check should_exit while lifespan startup runs).
            # Cancel it outright instead of waiting shutdown_timeout for nothing.
            if self._server_task is not None and not self._server_task.done():
                self._server_task.cancel()
            await self.stop()
            raise

    async def stop(self) -> None:
        """Stop the server and clean up all resources including WebSocket connections.

        Safe to call more than once, and after a failed ``start()``. If the
        task calling ``stop()`` is cancelled, the cancellation propagates after
        the port has been released.
        """
        try:
            if self._client:
                await self._client.close()
                self._client = None

            if self._server_task:
                task = self._server_task
                try:
                    if self._server:
                        self._server.should_exit = True

                    await asyncio.wait_for(task, timeout=self.shutdown_timeout)

                except TimeoutError:
                    logger.error("Timeout waiting for server shutdown on host %s port %s", self._host, self._port)
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # The caller of stop() is being cancelled: never swallow that.
                    logger.debug("Server task was cancelled")
                except Exception:
                    # A failed startup is reported by start(); anything else is not actionable here.
                    logger.debug("Server task ended with an error", exc_info=True)
                finally:
                    self._server_task = None
                    self._server = None
        finally:
            if self._port:
                _port_generator.release_port(self._port)
                self._port = None

    @property
    def base_url(self) -> str:
        if not self._port:
            raise RuntimeError("Server is not running")
        return f"http://{self._host}:{self._port}"

    async def __aenter__(self) -> "AsyncTestServer":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    @property
    def client(self) -> AsyncTestClient:
        if not self._client:
            raise RuntimeError("Server is not running")
        return self._client


@asynccontextmanager
async def create_test_server(
    lifespan: Lifespan[AppType] | None = None,
) -> AsyncGenerator[AsyncTestServer, None]:
    """Create and manage a TestServer instance with proper lifecycle"""
    server = AsyncTestServer(lifespan=lifespan)
    try:
        await server.start()
        yield server
    finally:
        await server.stop()
