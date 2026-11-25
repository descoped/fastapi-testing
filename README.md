# FastAPI Testing

[![Build Status](https://github.com/descoped/fastapi-testing/actions/workflows/build-test.yml/badge.svg)](https://github.com/descoped/fastapi-testing/actions/workflows/build-test.yml)
[![Coverage](https://codecov.io/gh/descoped/fastapi-testing/branch/master/graph/badge.svg)](https://codecov.io/gh/descoped/fastapi-testing)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Release](https://img.shields.io/github/v/release/descoped/fastapi-testing)](https://github.com/descoped/fastapi-testing/releases)

A lightweight, async-first testing framework designed specifically for FastAPI applications. This library provides a simple way to write integration tests for FastAPI applications with proper lifecycle management and async support.

## Features

- Async-first design for modern Python applications
- Automatic port management for test servers
- Clean lifecycle management with context managers
- Built-in HTTP and WebSocket client support
- Proper cleanup of resources after tests, with a fast stop by default (graceful drain is opt-in)
- Support for FastAPI's lifespan events
- Type-safe with full typing support

## Installation

```bash
uv add fastapi-testing
```

Or with pip:
```bash
pip install fastapi-testing
```

## Quick Start

Here's a simple example of how to test a FastAPI endpoint:

```python
import pytest
from fastapi import FastAPI
from fastapi_testing import create_test_server


@pytest.mark.asyncio
async def test_hello_world():
    async with create_test_server() as server:

        @server.app.get("/hello")
        async def hello():
            return {"message": "Hello, World!"}

        response = await server.client.get("/hello")
        await response.expect_status(200)
        data = await response.json()
        assert data["message"] == "Hello, World!"
```

## Architecture

The following sequence diagram illustrates the lifecycle of a test using this framework:

```mermaid
sequenceDiagram
    participant Test
    participant AsyncTestServer
    participant PortGenerator
    participant Lifespan as Lifespan (owned task)
    participant UvicornServer
    participant AsyncTestClient

    Test->>+AsyncTestServer: create_test_server()
    AsyncTestServer->>+PortGenerator: bind()
    PortGenerator-->>-AsyncTestServer: listening socket on a free port

    AsyncTestServer->>+Lifespan: lifespan startup (through the full ASGI app)
    Lifespan-->>AsyncTestServer: started, with lifespan state

    AsyncTestServer->>+UvicornServer: serve(sockets=[socket]), lifespan off
    UvicornServer-->>AsyncTestServer: ready
    AsyncTestServer-->>-Test: server instance (server.client ready)

    Note over Test,AsyncTestClient: Test execution happens here

    Test->>+AsyncTestServer: stop() (context exit)
    AsyncTestServer->>AsyncTestClient: close()
    AsyncTestServer->>UvicornServer: stop HTTP ("fast": close connections now; "graceful": drain)
    UvicornServer-->>-AsyncTestServer: stopped
    AsyncTestServer->>Lifespan: lifespan shutdown
    Lifespan-->>-AsyncTestServer: shut down
    AsyncTestServer->>PortGenerator: close socket, release port
    AsyncTestServer-->>-Test: cleanup complete
```

## Key Components

### AsyncTestServer

The `AsyncTestServer` class is the core component that manages the lifecycle of your test FastAPI application:

```python
from fastapi_testing import AsyncTestServer

server = AsyncTestServer()
await server.start()
# Use server.app to define routes
# Use server.client to make requests
await server.stop()
```

### Context Manager

The recommended way to use the test server is with the async context manager:

```python
from fastapi_testing import create_test_server

async with create_test_server() as server:
    # Your test code here
    pass  # Server automatically starts and stops
```

### Testing Your Own Application

Pass the application your project builds; its own lifespan runs, and its routes are served:

```python
from myproject.main import create_app

async with create_test_server(app=create_app()) as server:
    response = await server.client.get("/health")
    await response.expect_status(200)
```

`app` and `lifespan` are alternatives: `lifespan` is for the empty `FastAPI` app the server creates when no `app` is
given, so passing both raises `ValueError`.

### Timeouts

| Parameter | Default | Bounds |
|---|---|---|
| `startup_timeout` | 30 s | the application's lifespan startup and the server's own startup, together |
| `shutdown_timeout` | 10 s | the lifespan shutdown; in `"graceful"` mode also open connections finishing on stop |
| `client_timeout` | 30 s | each request with `server.client`, and opening its WebSocket connections |

```python
async with create_test_server(startup_timeout=60.0, client_timeout=5.0) as server:
    ...
```

### Fast and graceful shutdown

`stop()` releases every resource in both modes: connections, in-flight requests, the application's lifespan shutdown,
the socket, the port and the signal handlers. The modes differ only in how long open connections get:

| `shutdown` | Open connections and in-flight requests | Cost of `stop()` |
|---|---|---|
| `"fast"` (default) | closed at once, which the application sees as its client disconnecting; requests still running after 0.5 s are cancelled | about a millisecond |
| `"graceful"` | stop accepting and give them `shutdown_timeout` to finish, as uvicorn does in production | about 200 ms (uvicorn's polling), or until they finish |

Fast suits test suites that start a server per test. Use graceful for a test that checks what the application does
while it shuts down, such as an in-flight request still completing:

```python
async with create_test_server(shutdown="graceful") as server:
    ...

set_config(Config(shutdown="graceful"))  # for every server; or FASTAPI_TESTING_SHUTDOWN=graceful with Config.from_env()
```

A streaming response (for example SSE) still open at stop ends the same way in both modes, and the same way as when
a browser closes it: Starlette cancels the stream, so cleanup in its generator runs but is cut short at its first
`await` unless it shields itself (`anyio.CancelScope(shield=True)`).

### AsyncTestClient

The `AsyncTestClient` provides methods for making HTTP requests to your test server:

```python
# Available HTTP methods
await server.client.get("/path")
await server.client.post("/path", json=data)
await server.client.put("/path", json=data)
await server.client.delete("/path")
await server.client.patch("/path", json=data)
```

### Response Assertions

The `AsyncTestResponse` class provides convenient methods for assertions:

```python
response = await server.client.get("/path")
await response.expect_status(200)  # Raises AssertionError with the actual status and body, also under python -O
data = await response.json()  # Get JSON response
text = await response.text()  # Get text response
raw = response.httpx_response  # The underlying httpx.Response, for anything else
```

### Streaming Responses and Server-Sent Events

`client.stream()` makes a streaming request and yields the `httpx.Response` before its body is read. The response is
closed when the block exits, so breaking out of an endless stream is safe:

```python
async with server.client.stream("GET", "/events") as response:
    assert response.status_code == 200
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            ...
```

### The Server's Client

`server.client` is shared for the server's lifetime and closed by `stop()`. `async with server.client as client:`
does not close it, so it can be used any number of times in a test. A client you create yourself,
`async with AsyncTestClient(server.base_url) as client:`, is closed when the block exits. WebSocket connections opened
through `server.client` likewise stay open until you close them or `stop()` runs; close one explicitly with
`await ws_response.websocket().close()` when a test needs the server to see the disconnect.

### WebSocket Testing

Test WebSocket endpoints with full protocol support:

```python
@pytest.mark.asyncio
async def test_mixed_protocols(test_server):
    # Define HTTP endpoint
    @test_server.app.get("/api/data")
    async def get_data():
        return {"status": "ok"}

    # Define WebSocket endpoint
    @test_server.app.websocket("/ws/echo")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept(subprotocol="test-protocol")
        while True:
            try:
                message = await websocket.receive()
                if "text" in message:
                    data = json.loads(message["text"])
                    await websocket.send_json(data)
                elif "bytes" in message:
                    await websocket.send_bytes(message["bytes"])
            except WebSocketDisconnect:
                return

    # Test HTTP endpoint
    http_response = await test_server.client.get("/api/data")
    assert http_response.status_code == 200

    # Configure WebSocket
    config = WebSocketConfig(subprotocols=["test-protocol"], ping_interval=20.0, ping_timeout=20.0)

    # Test WebSocket endpoint
    ws_response = await test_server.client.websocket("/ws/echo", config)
    try:
        # Test JSON messages
        test_json = {"message": "test"}
        await test_server.client.ws.send_json(ws_response, test_json)
        response = await test_server.client.ws.receive_json(ws_response)
        assert response == test_json

        # Test binary messages
        test_data = b"binary test"
        await test_server.client.ws.send_binary(ws_response, test_data)
        response = await test_server.client.ws.receive_binary(ws_response)
        assert response == test_data
    finally:
        await ws_response.websocket().close()
```

#### WebSocket Message Operations

The WebSocketHelper provides comprehensive message handling:

```python
# Send Operations
await client.ws.send_text(ws_response, "message")
await client.ws.send_json(ws_response, {"key": "value"})
await client.ws.send_binary(ws_response, b"data")

# Receive Operations
text = await client.ws.receive_text(ws_response)
json_data = await client.ws.receive_json(ws_response)
binary = await client.ws.receive_binary(ws_response)

# Message Expectations
await client.ws.expect_message(ws_response, expected="message", timeout=1.0)

# Collect messages until none arrives within the timeout or the server closes normally (an abnormal close raises)
messages = await client.ws.drain_messages(ws_response, timeout=1.0)
```

#### WebSocket Configuration

Configure connections with various options:

```python
ws_config = WebSocketConfig(
    subprotocols=["protocol"],  # Supported subprotocols
    compression=None,  # Compression algorithm
    extra_headers={},  # Additional headers
    ping_interval=20.0,  # Keep-alive interval
    ping_timeout=20.0,  # Ping timeout
    max_size=2**20,  # Max message size (1MB)
    max_queue=32,  # Max queued messages
    timeout=30.0,  # Connection timeout
)
```

A field left as `None` uses the default: `max_size` and `max_queue` come from the active `Config`, everything else
from websockets. To pass `None` itself to websockets, for example to disable keepalive pings, use `options`:

```python
ws_response = await server.client.websocket("/ws", options={"ping_interval": None})
```

## Advanced Usage

### Advanced Server Configuration

You can customize the server lifecycle using a reusable test fixture. The lifespan is an
`@asynccontextmanager` function, the form Starlette's `Lifespan` type describes, so type checkers
accept it:

```python
@pytest.fixture
async def test_server(
    test_settings: Settings, transaction_manager: TransactionManager
) -> AsyncGenerator[AsyncTestServer, None]:
    """Create test server with overridden settings and database connection"""

    @asynccontextmanager
    async def custom_lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Wire up test-specific dependencies
        app.dependency_overrides.update(
            {
                get_settings: lambda: test_settings,
                get_transaction_manager: lambda: transaction_manager,
                get_db_pool: lambda: transaction_manager.pool,
            }
        )

        yield  # Server handles requests during this period

        # Cleanup after tests complete
        await db.cleanup()

    async with create_test_server(lifespan=custom_lifespan) as server:
        yield server
```

### Testing Routes and Routers

You can test entire routers and complex route configurations:

```python
@pytest.mark.asyncio
async def test_api(test_server: AsyncTestServer):
    # Register routes/routers
    test_server.app.include_router(your_router)

    # Make requests
    response = await test_server.client.get("/your-endpoint")
    await response.expect_status(200)

    # Test concurrent requests
    responses = await asyncio.gather(*[test_server.client.get("/endpoint") for _ in range(5)])

    for response in responses:
        await response.expect_status(200)
```

### Lifecycle Management

You can define setup and cleanup operations using FastAPI's lifespan:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup
    print("Starting server")
    yield
    # Cleanup
    print("Shutting down server")


async with create_test_server(lifespan=lifespan) as server:
    # Your test code here
    pass
```

### Concurrent Requests

The framework supports testing concurrent requests:

```python
import asyncio

async with create_test_server() as server:

    @server.app.get("/ping")
    async def ping():
        return {"status": "ok"}

    responses = await asyncio.gather(*[server.client.get("/ping") for _ in range(5)])
```

## Configuration

You can customize the server behavior:

```python
server = AsyncTestServer(
    app=create_app(),  # your application; omit it for an empty FastAPI app (or pass lifespan=)
    startup_timeout=30.0,  # seconds for lifespan startup plus the server becoming ready
    shutdown_timeout=10.0,  # seconds for lifespan shutdown; in "graceful" mode also open connections
    client_timeout=30.0,  # seconds for each request made with server.client
    shutdown="fast",  # or "graceful"; None (the default) uses the active Config
)
```

`create_test_server()` accepts the same arguments.

### Lifecycle guarantees

`AsyncTestServer` owns the whole lifecycle; uvicorn only serves connections:

- **Socket:** the server binds its own listening socket on `127.0.0.1` to a free port in the configured range, so no
  other process can take the port between choosing and binding it. `stop()` always closes it.
- **Lifespan:** the application's lifespan runs in a task the server owns, through the full ASGI application (so
  middleware sees it). State yielded by the lifespan reaches `request.state` and `websocket.state`. You can assign
  `create_test_server(app=create_app())`, or assign `server.app = create_app()` before `start()`; its lifespan is the
  one that runs.
- **Startup:** lifespan startup runs first, then the server starts serving. If either fails or takes longer than
  `startup_timeout`, `start()` releases everything acquired so far and raises `ServerStartupError`, chained to the
  application's own exception.
- **Shutdown:** `stop()` closes the client, stops HTTP (in the default `"fast"` mode it closes open connections at
  once; in `"graceful"` mode they get `shutdown_timeout` to finish, see
  [Fast and graceful shutdown](#fast-and-graceful-shutdown)), runs the lifespan shutdown, closes the socket and
  releases the port. If the lifespan shutdown fails or times out, `stop()` raises `ServerShutdownError` after all of
  that is done. If `stop()` is cancelled or a step fails, the remaining steps still run, without waiting, before the
  error propagates; on that path the lifespan is cancelled rather than shut down, so the code after its `yield` does
  not run.
- **Errors from the test come first:** when the body of `async with create_test_server()` (or
  `async with AsyncTestServer()`) raises, that exception is what the test reports. A `ServerShutdownError` from the
  cleanup is attached to it as a note instead of replacing it, and a cancelled test stays cancelled.
- **Signals:** while serving, SIGINT and SIGTERM stop the server, as in uvicorn. Overlapping servers share the
  process handlers correctly whatever order they stop in, and the original handlers come back when the last
  server stops, so Ctrl-C keeps interrupting the test run.

### Global settings

Port range, HTTP connection limits, WebSocket limits and connection retries are set with a `Config`. Activate it
with `set_config()`, for example in `conftest.py`; it applies to every server and connection created afterwards.
WebSocket connects retry connection errors (refused, closed early, timed out) `ws_retry_attempts` times, `ws_retry_delay` seconds
apart; a rejected handshake such as a 403 or 404, or a TLS failure, fails at once. Values are checked when the `Config`
is made: sizes, limits and attempts must be at least 1 (`http_max_keepalive` at least 0), `ws_retry_delay` a finite
number of at least 0, and the port range from 1 to 65535 with the start not above the end; anything else raises
`ValueError`:

```python
from fastapi_testing import Config, set_config

set_config(Config(port_range_start=9000, port_range_end=9500, ws_retry_attempts=5))

# or read FASTAPI_TESTING_* environment variables, e.g. FASTAPI_TESTING_PORT_RANGE_START=9000.
# Unknown names and invalid values are ignored with a warning; strict=True raises ValueError instead.
set_config(Config.from_env(strict=True))

# or load a JSON or TOML file; pyproject.toml is read from its [tool.fastapi-testing] table
set_config(Config.from_file("pyproject.toml"))
```

```toml
[tool.fastapi-testing]
port_range_start = 9000
port_range_end = 9500
ws_max_message_size = 2097152
```

`get_config()` returns the active configuration. Assigning to an imported `global_config` name has no effect on the
library; use `set_config()` instead.

## Best Practices

1. Always use the async context manager (`create_test_server`) when possible
2. Clean up resources in your tests, especially when managing state
3. Use pytest.mark.asyncio for your test functions
4. Handle exceptions appropriately in your tests
5. Use type hints to catch potential issues early

## Testing

The library is tested on Python 3.11 to 3.15 with about 97% code coverage, and type-checked with pyright and ty.

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=src/fastapi_testing --cov-report=term-missing

# Run specific test categories
uv run pytest tests/test_server.py          # Server functionality
uv run pytest tests/test_websockets.py     # WebSocket support
uv run pytest tests/test_config.py         # Configuration tests
```

### Test Structure

The test suite includes:

- **Integration tests** - Real FastAPI applications with actual network connections
- **Configuration tests** - Environment variable loading and validation
- **Error handling tests** - Edge cases and failure scenarios
- **WebSocket tests** - Real-time bidirectional communication
- **Lifecycle tests** - Server startup, shutdown, and resource management
- **Concurrent request tests** - Multiple simultaneous connections

All tests follow modern async/await patterns and avoid mocks to ensure real-world reliability.

## Error Handling

The framework provides clear error messages for common issues:

- Server startup failures: if the app's lifespan startup raises, no port is free, or startup times out, `start()`
  raises `ServerStartupError` (a `RuntimeError`), chained to the original exception, and releases everything
- Server shutdown failures: if the app's lifespan shutdown raises or hangs, `stop()` raises `ServerShutdownError`
  (a `RuntimeError`) after releasing everything, so the test that caused it fails. If the test itself already
  failed, its own exception is raised instead, with the shutdown error as a note
- Connection errors
- Invalid request formats

## Limitations

- Only supports async test cases
- Requires Python 3.11+
- Designed specifically for FastAPI applications

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
