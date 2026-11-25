---
name: write-tests
description: Write integration tests for FastAPI applications with fastapi-testing, which serves the real app on a real uvicorn server with real HTTP, Server-Sent Events and WebSocket connections. Use it whenever a project depends on fastapi-testing, or the user wants FastAPI tests that go over the network instead of TestClient/ASGITransport — adding the library, writing pytest fixtures for a test server, overriding dependencies, injecting fakes or seeded state, faking an upstream HTTP service, testing SSE or WebSocket endpoints, choosing fast or graceful shutdown, or prototyping tests in a Jupyter notebook. Use it even when the user only says "write tests for my API" in a FastAPI project that has fastapi-testing installed.
---

# Writing tests with fastapi-testing

fastapi-testing binds a free port, runs the application's lifespan, serves the app with uvicorn, and gives you an
async client. A test talks to the app over real sockets, so middleware, lifespan state, streaming, WebSockets and
outbound HTTP calls all behave as in production. The test still controls everything the app depends on, through
FastAPI's `dependency_overrides`, so the app becomes a simulation you set up per test.

This skill describes fastapi-testing 0.5. Check the installed version first (`uv pip show fastapi-testing`, or the
lock file). For 0.3 or 0.4 read `../troubleshoot/references/migration.md`, because the API and shutdown behaviour
changed.

## The core API in one screen

```python
from fastapi_testing import create_test_server, AsyncTestServer, WebSocketConfig

async with create_test_server(app=create_app()) as server:  # starts, then stops on exit
    response = await server.client.get("/items/1")  # also post/put/patch/delete/request
    await response.expect_status(200)  # async; raises AssertionError with the body
    data = await response.json()  # async, like text()
    response.httpx_response  # the underlying httpx.Response

    async with server.client.stream("GET", "/events") as r:  # SSE or chunked bodies
        async for line in r.aiter_lines():
            ...

    ws = await server.client.websocket("/ws")  # WebSocket, with server.client.ws helpers
    await server.client.ws.send_json(ws, {"n": 1})
    assert await server.client.ws.receive_json(ws) == {...}
    await ws.websocket().close()

    server.base_url  # "http://127.0.0.1:<port>", for other clients
    server.app  # the FastAPI app being served
```

Without `app=`, the server creates an empty `FastAPI()` and you add routes to `server.app`, which is handy for quick
prototypes and for fake upstream services. `lifespan=` gives that empty app a lifespan; `app` and `lifespan` together
raise `ValueError`. Full signatures, options and errors: `references/api.md`.

## How to write the tests

1. **Serve the project's real app.** Build it with the project's own factory (`create_app()`), or import its `app`
   object, and pass it as `create_test_server(app=...)`. Its lifespan runs. Don't rebuild routes in the test; the
   point is to test what ships.
2. **Swap dependencies, not code.** Set `app.dependency_overrides[get_x] = lambda: fake_x` before the server starts.
   Override settings to point outbound calls at a fake, the database dependency at a test-owned pool or transaction,
   the clock or ID generator at something deterministic. This is what makes the app a simulation you control.
3. **Put the server in a function-scoped fixture that depends on the resources it uses.** pytest then stops the
   server before it closes those resources. Every fixture closes what it opens.
4. **Arrange through what the test owns, assert through the API.** Seed state in the fake or the test database, call
   the endpoint over HTTP, assert on the response, and check side effects in the resource the test owns.
5. **Fake an upstream HTTP service with a second server.** `create_test_server()` with inline routes is a real HTTP
   server; point the app's settings at its `base_url`. The app's own HTTP client code then runs unchanged.

`examples/` is a complete, runnable suite showing all of this: `example_app.py` (an app with a factory, settings,
a lifespan resource, an outbound call, SSE and a WebSocket), `conftest.py` (the fixtures) and `test_example_*.py`.
Copy from it rather than writing from memory. The library's CI runs these files, so they match the release.

## Rules that prevent the expensive failures

These come from real suites that hung or leaked. Each one is cheap to follow and costly to debug later.

- **Keep async resources function-scoped.** pytest-asyncio gives every test its own event loop. A pool, client or
  connection created in one test's loop and reused in another hangs or fails, often only at teardown. Never cache a
  loop-bound object on a session-scoped fixture, and never "close and reuse" one. Details and patterns:
  `references/fixtures.md`.
- **Close everything a fixture opens, after the server stops.** A database pool that is still opening connections
  when the test ends can hang the event loop's shutdown. Open pools with `wait=True` (psycopg) or the library's
  equivalent, and close them in the fixture's `finally`.
- **Check what the app's own lifespan opens.** `dependency_overrides` don't reach the lifespan, so with
  `app=create_app()` it still opens its pools and clients from its settings, often a production default. Point
  those settings at test infrastructure, or use what the lifespan created: `references/fixtures.md`, "What the
  app's own lifespan opens".
- **A server per test is cheap.** The default fast stop takes about a millisecond, and a whole start, request and
  stop about 6 ms. Prefer per-test servers over shared ones; share only with a deliberate loop scope (see
  `references/fixtures.md`).
- **Leave `shutdown` at its default `"fast"`.** It closes open connections at once (the app sees a client
  disconnect), gives running requests 0.5 s, then cancels them, and still runs the lifespan shutdown. Use
  `shutdown="graceful"` only for a test about shutdown itself, such as an in-flight request completing during
  `stop()`. It costs about 200 ms per stop.
- **Don't add sleeps to fix timing.** If a test needs a sleep to pass, a resource is outliving its owner. Use the
  troubleshoot skill.
- **Close WebSockets and leave streams through `async with`.** Both are also closed by `stop()`, but closing them in
  the test makes the server see the disconnect when the test expects it.

Choosing an approach for a particular kind of test (simulation through overrides, fake upstreams, SSE, WebSocket,
concurrency, startup failures, shutdown behaviour): `references/strategies.md`.

## Project setup

```bash
uv add --dev fastapi-testing pytest pytest-asyncio     # or: pip install fastapi-testing pytest pytest-asyncio
```

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"                        # async tests and fixtures without decorators
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["."]                           # when tests import the app package and it isn't installed
```

Python 3.11 or later. The package ships type information, so pyright and mypy check test code against it.

## Notebooks

The library works in Jupyter with top-level `await`, which is a good way to explore an API before writing the
pytest file: `references/notebooks.md`.

## When something goes wrong

Hangs, teardown errors, `ResourceWarning`, "Exception in ASGI application", port or signal problems, or breakage
after an upgrade: use the `troubleshoot` skill in this plugin.
