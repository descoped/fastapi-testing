FastAPI Testing
===============

A lightweight, async-first testing framework designed specifically for FastAPI applications. This library provides a simple way to write integration tests for FastAPI applications with proper lifecycle management and async support.

Features
------------

- Async-first design for modern Python applications
- Automatic port management for test servers
- Clean lifecycle management with context managers
- Built-in HTTP client with async support
- Proper cleanup of resources after tests
- Support for FastAPI's lifespan events
- Type-safe with full typing support

Installation
----------------

.. code-block:: bash

   pip install fastapi-testing

Quick Start
--------------

Here's a simple example of how to test a FastAPI endpoint:

.. code-block:: python

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

Architecture
----------------

The framework follows a lifecycle-based architecture with proper resource management:

1. Test server initialization
2. Port allocation
3. Server startup
4. Test execution
5. Cleanup and shutdown

Key Components
------------------

TestServer
~~~~~~~~~~~~~~

The ``TestServer`` class is the core component that manages the lifecycle of your test FastAPI application:

.. code-block:: python

   from fastapi_testing import TestServer

   server = TestServer()
   await server.start()
   # Use server.app to define routes
   # Use server.client to make requests
   await server.stop()

Context Manager
~~~~~~~~~~~~~~~~~~

The recommended way to use the test server is with the async context manager:

.. code-block:: python

   from fastapi_testing import create_test_server

   async with create_test_server() as server:
       # Your test code here
       pass  # Server automatically starts and stops

TestClient
~~~~~~~~~~~~~

The ``TestClient`` provides methods for making HTTP requests to your test server:

.. code-block:: python

   # Available HTTP methods
   await server.client.get("/path")
   await server.client.post("/path", json=data)
   await server.client.put("/path", json=data)
   await server.client.delete("/path")
   await server.client.patch("/path", json=data)

Response Assertions
~~~~~~~~~~~~~~~~~~~~

The ``TestResponse`` class provides convenient methods for assertions:

.. code-block:: python

   response = await server.client.get("/path")
   await response.expect_status(200)  # Assert status code
   data = await response.json()       # Get JSON response
   text = await response.text()       # Get text response

Advanced Usage
----------------

Lifecycle Management
~~~~~~~~~~~~~~~~~~~~

You can define setup and cleanup operations using FastAPI's lifespan:

.. code-block:: python

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

Concurrent Requests
~~~~~~~~~~~~~~~~~~~

The framework supports testing concurrent requests:

.. code-block:: python

   import asyncio

   async with create_test_server() as server:
       @server.app.get("/ping")
       async def ping():
           return {"status": "ok"}

       responses = await asyncio.gather(*[
           server.client.get("/ping")
           for _ in range(5)
       ])

Configuration
~~~~~~~~~~~~~~

You can customize the server behavior:

.. code-block:: python

   server = TestServer(
       startup_timeout=30.0,    # Seconds to wait for server startup
       shutdown_timeout=10.0,   # Seconds to wait for server shutdown
   )

Best Practices
----------------

1. Always use the async context manager (``create_test_server``) when possible
2. Clean up resources in your tests, especially when managing state
3. Use pytest.mark.asyncio for your test functions
4. Handle exceptions appropriately in your tests
5. Use type hints to catch potential issues early

Error Handling
----------------

The framework provides clear error messages for common issues:

- Server startup timeout
- Port allocation failures
- Connection errors
- Invalid request formats

Limitations
--------------

- Only supports async test cases
- Requires Python 3.11+
- Designed specifically for FastAPI applications

Contributing
---------------

Contributions are welcome! Please feel free to submit a Pull Request.