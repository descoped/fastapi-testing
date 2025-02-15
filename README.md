# FastAPI Testing

A lightweight, async-first testing framework designed specifically for FastAPI applications. This library provides a simple way to write integration tests for FastAPI applications with proper lifecycle management and async support.

## Features

- Async-first design for modern Python applications
- Automatic port management for test servers
- Clean lifecycle management with context managers
- Built-in HTTP client with async support
- Proper cleanup of resources after tests
- Support for FastAPI's lifespan events
- Type-safe with full typing support

## Installation

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
    participant UvicornServer
    participant FastAPI
    participant AsyncTestClient

    Test->>+AsyncTestServer: create_test_server()
    AsyncTestServer->>+PortGenerator: get_port()
    PortGenerator-->>-AsyncTestServer: available port
    
    AsyncTestServer->>+UvicornServer: initialize
    UvicornServer->>FastAPI: configure
    
    AsyncTestServer->>+UvicornServer: start()
    UvicornServer->>FastAPI: startup event
    UvicornServer-->>AsyncTestServer: server ready
    
    AsyncTestServer->>+AsyncTestClient: initialize
    AsyncTestClient-->>-AsyncTestServer: client ready
    AsyncTestServer-->>-Test: server instance
    
    Note over Test,AsyncTestClient: Test execution happens here
    
    Test->>+AsyncTestServer: cleanup (context exit)
    AsyncTestServer->>+AsyncTestClient: close()
    AsyncTestClient-->>-AsyncTestServer: closed
    
    AsyncTestServer->>+UvicornServer: shutdown
    UvicornServer->>FastAPI: shutdown event
    UvicornServer-->>-AsyncTestServer: shutdown complete
    
    AsyncTestServer->>+PortGenerator: release_port()
    PortGenerator-->>-AsyncTestServer: port released
    AsyncTestServer-->>-Test: cleanup complete
```

The diagram shows how the different components interact during:
1. Test server initialization
2. Port allocation
3. Server startup
4. Test execution
5. Cleanup and shutdown

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
await response.expect_status(200)  # Assert status code
data = await response.json()       # Get JSON response
text = await response.text()       # Get text response
```

## Advanced Usage

### Advanced Server Configuration

You can customize the server lifecycle using a reusable test fixture:

```python
@pytest.fixture
async def test_server(
    test_settings: Settings,
    transaction_manager: TransactionManager
) -> AsyncGenerator[AsyncTestServer, None]:
    """Create test server with overridden settings and database connection"""

    async def custom_lifespan(app: AppType) -> AsyncGenerator[None, Any]:
        # Wire up test-specific dependencies
        app.dependency_overrides.update({
            get_settings: lambda: test_settings,
            get_transaction_manager: lambda: transaction_manager,
            get_db_pool: lambda: transaction_manager.pool
        })

        yield  # Server handles requests during this period

        # Cleanup after tests complete
        await db.cleanup()

    async with create_test_server(lifespan=custom_lifespan) as server:
        yield server
```

> This gives you enormous flexibility in setting up your test environment.

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
    responses = await asyncio.gather(*[
        test_server.client.get("/endpoint")
        for _ in range(5)
    ])
    
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

    responses = await asyncio.gather(*[
        server.client.get("/ping")
        for _ in range(5)
    ])
```

### Configuration

You can customize the server behavior:

```python
server = AsyncTestServer(
    startup_timeout=30.0,    # Seconds to wait for server startup
    shutdown_timeout=10.0,   # Seconds to wait for server shutdown
)
```

## Best Practices

1. Always use the async context manager (`create_test_server`) when possible
2. Clean up resources in your tests, especially when managing state
3. Use pytest.mark.asyncio for your test functions
4. Handle exceptions appropriately in your tests
5. Use type hints to catch potential issues early

## Error Handling

The framework provides clear error messages for common issues:

- Server startup timeout
- Port allocation failures
- Connection errors
- Invalid request formats

## Limitations

- Only supports async test cases
- Requires Python 3.11+
- Designed specifically for FastAPI applications

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
