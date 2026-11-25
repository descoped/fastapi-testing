# Test strategies with fastapi-testing

Pick the section for the kind of test you are writing. Every strategy here uses the standard function-scoped server
fixture from `fixtures.md`; the runnable versions are in `../examples/`.

## Contents
- [When a real server is the right tool](#when-a-real-server-is-the-right-tool)
- [The app as a simulation](#the-app-as-a-simulation)
- [Faking an upstream HTTP service](#faking-an-upstream-http-service)
- [Authentication](#authentication)
- [Server-Sent Events and streaming](#server-sent-events-and-streaming)
- [WebSockets](#websockets)
- [Concurrency](#concurrency)
- [Background work](#background-work)
- [Startup and shutdown behaviour](#startup-and-shutdown-behaviour)

## When a real server is the right tool

Starlette's `TestClient` and httpx's `ASGITransport` call the app in-process, without sockets. fastapi-testing serves
it with uvicorn on a real port. Reach for it when the behaviour under test depends on the network: streaming and
SSE (including client disconnects), WebSockets, the app's lifespan, middleware that reads the connection, several
clients at once, outbound calls to other servers, or code that itself needs a URL (a browser, a separate client
library, another process). For a pure function of request to response, either approach works; keep one style per
suite.

## The app as a simulation

Treat each test as a small world you build: the real app, with every dependency that reaches outside replaced.

| Dependency | Replace it with |
|---|---|
| settings | `Settings(...)` pointing at fakes and test URLs |
| database | a test-owned pool or engine (see `fixtures.md`), seeded by the test |
| outbound HTTP | a fake upstream server (below), or an in-memory fake client |
| clock, IDs, randomness | a fixed or stepping fake, so responses are deterministic |
| current user | a fixed user (see Authentication) |
| message queue, mailer, storage | in-memory fakes the test inspects afterwards |

```python
app = create_app()
app.dependency_overrides.update(
    {
        get_settings: lambda: Settings(payments_url=payments.base_url),
        get_clock: lambda: FrozenClock("2026-01-01T00:00:00Z"),
        get_mailer: lambda: mailer,  # a fixture-owned fake that records messages
    }
)
```

Arrange through the fakes, act over HTTP, assert on the response and on what the fakes recorded. A test reads like a
scenario: "given these orders and a payments service that times out, the checkout endpoint returns 503 and sends no
email".

## Faking an upstream HTTP service

A second `create_test_server()` is a real HTTP server, so the app's own client code (retries, timeouts, error
handling, serialisation) runs unchanged. Record what it receives, and make its behaviour switchable per test:

```python
@pytest.fixture
async def payments() -> AsyncGenerator[AsyncTestServer]:
    async with create_test_server() as upstream:
        upstream.app.state.calls = []
        upstream.app.state.fail = False

        @upstream.app.post("/charges")
        async def charge(request: Request):
            upstream.app.state.calls.append(await request.json())
            if upstream.app.state.fail:
                return JSONResponse({"error": "down"}, status_code=503)
            return {"id": "ch_1", "status": "succeeded"}

        yield upstream


async def test_checkout_survives_a_payments_outage(server, payments):
    payments.app.state.fail = True
    response = await server.client.post("/checkout", json={"order": "o1"})
    await response.expect_status(503)
    assert payments.app.state.calls == [{"order": "o1", "amount": 1000}]
```

To simulate a slow upstream, `await asyncio.sleep(...)` in the fake route and give the app a short timeout through
its settings. The app's `server` fixture takes `payments` as a parameter (as `examples/conftest.py` does with `rates_server`), so
pytest stops the app server before the fake it calls. Without that dependency the order is not guaranteed.

## Authentication

- **Testing endpoints, not auth**: override the dependency that resolves the user,
  `app.dependency_overrides[get_current_user] = lambda: User(id="u1", roles=["admin"])`.
- **Testing auth itself**: leave the dependency real, create a token with the app's own signing code and test
  settings, and send it with `headers={"Authorization": f"Bearer {token}"}`. Cover the missing, expired and wrong-role
  cases.

## Server-Sent Events and streaming

```python
async with server.client.stream("GET", "/events") as response:
    assert response.headers["content-type"].startswith("text/event-stream")
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            events.append(line.removeprefix("data: "))
            if len(events) == 3:
                break  # leaving the block closes the connection
```

- **Endless streams**: break out after the events you need. Closing the stream is a client disconnect to the app.
- **Cleanup on disconnect**: when a client disconnects, Starlette cancels the streaming generator, so code in its
  `finally` runs but is cut off at its first `await`. That is also true in production. To test cleanup that must
  finish (returning a connection, releasing a lock), the app has to shield it
  (`with anyio.CancelScope(shield=True): ...`). Assert on the effect, polling with a timeout as in
  [Background work](#background-work).
- **sse-starlette** works too; it detects server shutdown through uvicorn's signal handler, which fastapi-testing
  keeps in place.

## WebSockets

```python
ws = await server.client.websocket("/ws/chat")
try:
    await server.client.ws.send_json(ws, {"text": "hi"})
    await server.client.ws.expect_message(ws, expected={"text": "hi", "from": "u1"}, timeout=1.0)
finally:
    await ws.websocket().close()
```

- **Several clients** (broadcast, presence): open several connections from the same `server.client` and close each
  one in `finally`.
- **Server-initiated close**: `drain_messages(ws, timeout=...)` returns what arrived before a normal close. An
  abnormal close (1011 after the handler raised) raises, which is how a handler crash shows up in a test.
- **Auth and subprotocols**: `WebSocketConfig(extra_headers={...}, subprotocols=[...])`. A rejected handshake raises
  at once (`websockets.exceptions.InvalidStatus`); it is not retried.

## Concurrency

```python
responses = await asyncio.gather(*(server.client.get(f"/items/{i}") for i in range(50)))
```

The shared client allows `http_max_connections` (100) at once; set it with `Config` for more. For races (two
clients updating the same row), start both requests with `asyncio.gather` and assert on the final state through
the test-owned resource.

## Background work

FastAPI `BackgroundTasks` and work the app schedules run after the response is sent. Don't sleep a fixed time; wait
for the effect:

```python
async def eventually(check: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


await (await server.client.post("/signup", json=user)).expect_status(201)
await eventually(lambda: len(mailer.sent) == 1)
```

Background tasks belong to their request, so a fast stop gives them `request_grace` (0.5 s) to finish and then
cancels them. Wait for the effect before the test ends.

## Startup and shutdown behaviour

- **Startup failure**: `with pytest.raises(ServerStartupError) as info: async with create_test_server(app=app): ...`,
  then check `info.value.__cause__`, the app's own exception.
- **Lifespan shutdown**: its errors raise `ServerShutdownError` from `stop()`, so a test fails when the app's
  cleanup fails. To assert on cleanup, check the resource after the `async with` block.
- **In-flight requests at shutdown**: use `shutdown="graceful"` (and a `shutdown_timeout` large enough). Start the
  request as a task with your own `httpx.AsyncClient(base_url=server.base_url)`, wait until the handler has started
  (it sets an `asyncio.Event`; a fixed sleep races on a slow machine), call `await server.stop()`, and assert that
  the request completed. See `examples/test_example_lifecycle.py`. Under the default fast mode it would be cancelled after 0.5 s.
