import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List, Optional, Any, Set, AsyncGenerator, Dict, Union

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.applications import AppType
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.types import Lifespan
from websockets.asyncio.client import ClientConnection, connect
from websockets.protocol import State

logger = logging.getLogger(__name__)

# Configuration Constants
DEFAULT_WS_MESSAGE_SIZE = 2 ** 20  # 1MB
DEFAULT_WS_QUEUE_SIZE = 32
DEFAULT_KEEPALIVE_CONNS = 20
DEFAULT_MAX_CONNS = 100
DEFAULT_WS_RETRY_ATTEMPTS = 3
DEFAULT_WS_RETRY_DELAY = 1.0


class Config(BaseSettings):
    """
    Global configuration settings for fastapi-testing framework.

    Environment variables:
      - FASTAPI_TESTING_WS_MAX_MESSAGE_SIZE: Maximum size for websocket messages.
      - FASTAPI_TESTING_WS_QUEUE_SIZE: Maximum websocket queue size.
      - FASTAPI_TESTING_HTTP_MAX_KEEPALIVE: Maximum HTTP keepalive connections.
      - FASTAPI_TESTING_HTTP_MAX_CONNECTIONS: Maximum HTTP connections.
      - FASTAPI_TESTING_WS_RETRY_ATTEMPTS: Number of websocket retry attempts.
      - FASTAPI_TESTING_WS_RETRY_DELAY: Delay (in seconds) between websocket retries.
      - FASTAPI_TESTING_PORT_RANGE_START: Start port for testing servers.
      - FASTAPI_TESTING_PORT_RANGE_END: End port for testing servers.
    """
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FASTAPI_TESTING_")
    WS_MAX_MESSAGE_SIZE: int = DEFAULT_WS_MESSAGE_SIZE
    WS_MAX_QUEUE_SIZE: int = DEFAULT_WS_QUEUE_SIZE
    HTTP_MAX_KEEPALIVE: int = DEFAULT_KEEPALIVE_CONNS
    HTTP_MAX_CONNECTIONS: int = DEFAULT_MAX_CONNS
    WS_RETRY_ATTEMPTS: int = DEFAULT_WS_RETRY_ATTEMPTS
    WS_RETRY_DELAY: float = DEFAULT_WS_RETRY_DELAY
    PORT_RANGE_START: int = 8001
    PORT_RANGE_END: int = 9000


global_config = Config()


class InvalidResponseTypeError(Exception):
    """Exception raised when an operation is not supported for the response type."""
    pass


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
    """
    subprotocols: Optional[List[str]] = None
    compression: Optional[str] = None
    extra_headers: Optional[Dict[str, str]] = None
    ping_interval: Optional[float] = None
    ping_timeout: Optional[float] = None
    max_size: int = global_config.WS_MAX_MESSAGE_SIZE
    max_queue: int = global_config.WS_MAX_QUEUE_SIZE
    timeout: Optional[float] = None


class PortGenerator:
    """Manages port allocation for test servers using configuration from global settings.
    """

    def __init__(self, start: Optional[int] = None, end: Optional[int] = None):
        if start is None:
            start = global_config.PORT_RANGE_START
        if end is None:
            end = global_config.PORT_RANGE_END
        self.start = start
        self.end = end
        self.used_ports: Set[int] = set()

    @staticmethod
    def is_port_available(port: int) -> bool:
        from contextlib import closing
        import socket
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            try:
                sock.bind(('localhost', port))
                return True
            except (socket.error, OverflowError):
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

    def __init__(self, response: Union[httpx.Response, ClientConnection]):
        self._response = response
        self._is_websocket = isinstance(response, ClientConnection)

    async def json(self) -> Any:
        """Get JSON response (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError(
                "Cannot get JSON directly from WebSocket response. Use websocket() methods instead.")
        return await asyncio.to_thread(self._response.json)

    async def text(self) -> str:
        """Get text response (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError(
                "Cannot get text directly from WebSocket response. Use websocket() methods instead.")
        return await asyncio.to_thread(lambda: self._response.text)

    @property
    def status_code(self) -> int:
        """Get status code (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError("WebSocket connections don't have status codes")
        return self._response.status_code

    def websocket(self) -> ClientConnection:
        """Get WebSocket connection (WebSocket only)."""
        if not self._is_websocket:
            raise InvalidResponseTypeError("This response is not a WebSocket connection")
        return self._response

    async def expect_status(self, status_code: int) -> 'AsyncTestResponse':
        """Assert expected status code (HTTP only)."""
        if self._is_websocket:
            raise InvalidResponseTypeError("WebSocket connections don't have status codes")
        assert self._response.status_code == status_code, \
            f"Expected status {status_code}, got {self._response.status_code}"
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
            resp: AsyncTestResponse,
            expected: Union[str, dict, bytes],
            timeout: Optional[float] = None
    ) -> None:
        """Assert expected message is received within timeout."""
        ws = resp.websocket()
        try:
            message = await asyncio.wait_for(ws.recv(), timeout)
        except asyncio.TimeoutError as e:
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
    async def drain_messages(
            resp: AsyncTestResponse,
            timeout: Optional[float] = 0.1
    ) -> List[Any]:
        """Drain all pending messages from websocket queue."""
        ws = resp.websocket()
        messages = []
        try:
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout)
                messages.append(message)
        except asyncio.TimeoutError:
            pass
        return messages


class AsyncTestClient:
    """Async test client supporting both HTTP and WebSocket connections."""

    def __init__(
            self,
            base_url: str,
            timeout: float = 30.0,
            follow_redirects: bool = True
    ):
        self._base_url = base_url.rstrip('/')
        self._timeout = timeout
        self._websocket_connections: Set[ClientConnection] = set()

        limits = httpx.Limits(
            max_keepalive_connections=global_config.HTTP_MAX_KEEPALIVE,
            max_connections=global_config.HTTP_MAX_CONNECTIONS
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=follow_redirects,
            limits=limits,
            http2=True
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

    async def request(
            self,
            method: str,
            url: str,
            **kwargs: Any
    ) -> AsyncTestResponse:
        """Make HTTP request."""
        response = await self._client.request(method, url, **kwargs)
        return AsyncTestResponse(response)

    async def websocket(
            self,
            path: str,
            config: Optional[WebSocketConfig] = None,
            options: Optional[Dict[str, Any]] = None
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

        connect_kwargs: Dict[str, Any] = {
            'open_timeout': self._timeout,
            'max_size': global_config.WS_MAX_MESSAGE_SIZE,
            'max_queue': global_config.WS_MAX_QUEUE_SIZE
        }

        if config:
            if config.subprotocols:
                connect_kwargs['subprotocols'] = config.subprotocols
            if config.compression:
                connect_kwargs['compression'] = config.compression
            if config.extra_headers:
                connect_kwargs['additional_headers'] = config.extra_headers
            if config.ping_interval:
                connect_kwargs['ping_interval'] = config.ping_interval
            if config.ping_timeout:
                connect_kwargs['ping_timeout'] = config.ping_timeout
            if config.timeout:
                connect_kwargs['open_timeout'] = config.timeout

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
        return await self.request('GET', url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request('POST', url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request('PUT', url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request('DELETE', url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> AsyncTestResponse:
        return await self.request('PATCH', url, **kwargs)

    async def __aenter__(self) -> "AsyncTestClient":
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()


class UvicornTestServer(uvicorn.Server):
    """Uvicorn test server with startup event support."""

    def __init__(self, config: uvicorn.Config, startup_handler: asyncio.Event):
        super().__init__(config)
        self.startup_handler = startup_handler

    async def startup(self, sockets: Optional[List] = None) -> None:
        """Override startup to signal when ready."""
        await super().startup(sockets=sockets)
        self.startup_handler.set()


# Use the configurable PortGenerator instance
_port_generator = PortGenerator()


class AsyncTestServer:
    """Async test server with proper lifecycle management and WebSocket support."""

    def __init__(
            self,
            lifespan: Optional[Lifespan[AppType]] = None,
            startup_timeout: float = 30.0,
            shutdown_timeout: float = 10.0,
    ):
        self.app = FastAPI(lifespan=lifespan)
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self._startup_complete = asyncio.Event()
        self._shutdown_complete = asyncio.Event()
        self._server_task: Optional[asyncio.Task] = None
        self._port: Optional[int] = None
        self._host = "127.0.0.1"
        self._client: Optional[AsyncTestClient] = None
        self._server: Optional[UvicornTestServer] = None
        self._websocket_tasks: Set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the server asynchronously with proper lifecycle management."""
        if self._server_task is not None:
            raise RuntimeError("Server is already running")

        self._port = _port_generator.get_port()
        startup_handler = asyncio.Event()

        config = uvicorn.Config(
            app=self.app,
            host=self._host,
            port=self._port,
            log_level="error",
            loop="asyncio"
        )

        self._server = UvicornTestServer(config=config, startup_handler=startup_handler)

        self._server_task = asyncio.create_task(self._server.serve())

        try:
            await asyncio.wait_for(startup_handler.wait(), timeout=self.startup_timeout)

            self._client = AsyncTestClient(
                base_url=self.base_url,
                timeout=self.startup_timeout
            )

            self._startup_complete.set()

        except (asyncio.TimeoutError, Exception) as e:
            await self.stop()
            if isinstance(e, asyncio.TimeoutError):
                raise RuntimeError(
                    f"Server startup timed out on host {self._host} and port {self._port}"
                ) from e
            raise

    async def stop(self) -> None:
        """Stop the server and clean up all resources including WebSocket connections."""
        if not self._startup_complete.is_set():
            return

        # Cancel all WebSocket tasks
        for task in self._websocket_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._websocket_tasks, return_exceptions=True)
        self._websocket_tasks.clear()

        if self._client:
            await self._client.close()
            self._client = None

        if self._server_task:
            try:
                if self._server:
                    self._server.should_exit = True

                await asyncio.wait_for(self._server_task, timeout=self.shutdown_timeout)

            except asyncio.TimeoutError:
                logger.error(
                    f"Timeout waiting for server shutdown on host {self._host} port {self._port}"
                )
                if not self._server_task.done():
                    self._server_task.cancel()
                    await asyncio.gather(self._server_task, return_exceptions=True)
            except asyncio.CancelledError:
                logger.info("Server task cancelled successfully")
            finally:
                self._server_task = None

        if self._port:
            _port_generator.release_port(self._port)
            self._port = None

        self._shutdown_complete.set()

    @property
    def base_url(self) -> str:
        if not self._port:
            raise RuntimeError("Server is not running")
        return f"http://{self._host}:{self._port}"

    async def __aenter__(self) -> 'AsyncTestServer':
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
        lifespan: Optional[Lifespan[AppType]] = None,
) -> AsyncGenerator[AsyncTestServer, None]:
    """Create and manage a TestServer instance with proper lifecycle"""
    server = AsyncTestServer(lifespan=lifespan)
    try:
        await server.start()
        yield server
    finally:
        await server.stop()
