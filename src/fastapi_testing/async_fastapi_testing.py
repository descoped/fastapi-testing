import asyncio
import logging
import random
import socket
from contextlib import closing, asynccontextmanager
from typing import List, Optional, TypeVar, Any, Set, AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.applications import AppType
from starlette.types import Lifespan

logger = logging.getLogger(__name__)


class PortGenerator:
    def __init__(self, start: int = 8001, end: int = 9000):
        self.start = start
        self.end = end
        self.used_ports: Set[int] = set()

    def is_port_available(self, port: int) -> bool:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            try:
                sock.bind(('localhost', port))
                return True
            except (socket.error, OverflowError):
                return False

    def get_port(self) -> int:
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
        self.used_ports.discard(port)


_port_generator = PortGenerator()

T = TypeVar('T')


class UvicornTestServer(uvicorn.Server):
    """Uvicorn test server with startup event support"""

    def __init__(self, config: uvicorn.Config, startup_handler: asyncio.Event):
        super().__init__(config)
        self.startup_handler = startup_handler

    async def startup(self, sockets: Optional[List] = None) -> None:
        """Override startup to signal when ready"""
        await super().startup(sockets=sockets)
        self.startup_handler.set()


class AsyncTestServer:
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

    async def start(self) -> None:
        """Start the server asynchronously with proper lifecycle management"""
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

        # Create server task
        self._server_task = asyncio.create_task(self._server.serve())

        try:
            # Wait for startup with timeout
            await asyncio.wait_for(startup_handler.wait(), timeout=self.startup_timeout)

            # Initialize test client after server is confirmed running
            self._client = AsyncTestClient(
                base_url=self.base_url,
                timeout=self.startup_timeout
            )

            self._startup_complete.set()

        except (asyncio.TimeoutError, Exception) as e:
            await self.stop()
            if isinstance(e, asyncio.TimeoutError):
                raise RuntimeError("Server startup timed out")
            raise

    async def stop(self) -> None:
        """Stop the server and clean up resources"""
        if not self._startup_complete.is_set():
            return

        # Close the client first to avoid any ongoing requests during shutdown
        if self._client:
            await self._client.close()
            self._client = None

        if self._server_task:
            try:
                # Signal the server to shutdown gracefully
                if self._server:
                    self._server.should_exit = True

                # Wait for server task to complete
                await asyncio.wait_for(self._server_task, timeout=self.shutdown_timeout)

            except asyncio.TimeoutError:
                logger.error("Timeout waiting for the server to shut down gracefully. Forcibly canceling the task.")
                # Cancel if it didn't stop gracefully
                if not self._server_task.done():
                    self._server_task.cancel()
                    await asyncio.gather(self._server_task, return_exceptions=True)
            except asyncio.CancelledError:
                logger.info("Server task was cancelled successfully.")
            finally:
                self._server_task = None

        # Release the used port
        if self._port:
            _port_generator.release_port(self._port)
            self._port = None

        # Mark shutdown as complete
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
    def client(self) -> 'AsyncTestClient':
        if not self._client:
            raise RuntimeError("Server is not running")
        return self._client


class AsyncTestClient:
    def __init__(
            self,
            base_url: str,
            timeout: float = 30.0,
            follow_redirects: bool = True
    ):
        self._base_url = base_url.rstrip('/')
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=follow_redirects,
            limits=limits,
            http2=True
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def request(
            self,
            method: str,
            url: str,
            **kwargs: Any
    ) -> 'AsyncTestResponse':
        response = await self._client.request(method, url, **kwargs)
        return AsyncTestResponse(response)

    async def get(self, url: str, **kwargs: Any) -> 'AsyncTestResponse':
        return await self.request('GET', url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> 'AsyncTestResponse':
        return await self.request('POST', url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> 'AsyncTestResponse':
        return await self.request('PUT', url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> 'AsyncTestResponse':
        return await self.request('DELETE', url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> 'AsyncTestResponse':
        return await self.request('PATCH', url, **kwargs)


class AsyncTestResponse:
    def __init__(self, response: httpx.Response):
        self._response = response

    async def json(self) -> Any:
        return self._response.json()

    async def text(self) -> str:
        return self._response.text

    def status_code(self) -> int:
        return self._response.status_code

    async def expect_status(self, status_code: int) -> 'AsyncTestResponse':
        assert self._response.status_code == status_code, \
            f"Expected status {status_code}, got {self._response.status_code}"
        return self


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
