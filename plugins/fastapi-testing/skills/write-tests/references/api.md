# fastapi-testing 0.5 API reference

Everything below is importable from `fastapi_testing`.

## Contents
- [create_test_server and AsyncTestServer](#create_test_server-and-asynctestserver)
- [AsyncTestClient](#asynctestclient)
- [AsyncTestResponse](#asynctestresponse)
- [WebSockets](#websockets)
- [Config](#config)
- [Errors](#errors)
- [Lifecycle guarantees](#lifecycle-guarantees)

## create_test_server and AsyncTestServer

```python
create_test_server(
    lifespan=None,            # an @asynccontextmanager lifespan for the empty app the server creates
    startup_timeout=30.0,     # lifespan startup plus the server becoming ready, together
    shutdown_timeout=10.0,    # lifespan shutdown; in "graceful" mode also open connections finishing
    *,
    app=None,                 # your FastAPI app; its own lifespan runs. Excludes lifespan=
    client_timeout=30.0,      # each request with server.client, and opening its WebSockets
    shutdown=None,            # "fast" | "graceful"; None uses the active Config (default "fast")
)  # an async context manager yielding a started AsyncTestServer

AsyncTestServer(...)          # the same arguments; use when start and stop are in different places
    await server.start()      # binds a port, runs lifespan startup, serves; RuntimeError if already running
    await server.stop()       # releases everything; safe to call twice; the server can start again
    async with AsyncTestServer(app=app) as server: ...
    server.app                # the FastAPI app (created empty when app= is not given)
    server.client             # the shared AsyncTestClient; RuntimeError before start()
    server.base_url           # "http://127.0.0.1:<port>"; RuntimeError before start()
    server.request_grace      # 0.5: seconds a running request gets in a fast stop before it is cancelled
```

- **Routes on `server.app`** can be added before or after `start()`; a route added during a test is live at once.
- **Assigning `server.app = create_app()`** before `start()` also works, and that app's lifespan is the one that runs.
- **A bare async generator as `lifespan`** works at runtime, but type checkers reject it. Decorate it with
  `@asynccontextmanager`, which is the form Starlette's `Lifespan` type describes.
- **Lifespan state**: a lifespan that yields a dict (`yield {"pool": pool}`) makes each key available as
  `request.state.<key>` and `websocket.state.<key>`.

## AsyncTestClient

`server.client` is shared for the server's lifetime and closed by `stop()`; `async with server.client` does not close
it. A client you create yourself, `AsyncTestClient(server.base_url, timeout=30.0, follow_redirects=True)`, is closed
when its `async with` block exits.

```python
await client.get(url, **httpx_kwargs)  # also post, put, patch, delete
await client.request("OPTIONS", url, **httpx_kwargs)
async with client.stream("GET", url, **httpx_kwargs) as response:
    ...  # httpx.Response, body not yet read
await client.websocket(path, config=None, options=None)  # AsyncTestResponse wrapping the connection
client.ws  # WebSocketHelper
```

`**httpx_kwargs` are passed to httpx: `json=`, `params=`, `headers=`, `data=`, `files=`, `cookies=`, `timeout=`.
The client speaks HTTP/1.1. URLs are relative to `server.base_url`.

## AsyncTestResponse

```python
response.status_code  # int
response.headers  # httpx.Headers
await response.json()  # parsed body (async)
await response.text()  # body as text (async)
await response.expect_status(200)  # returns the response; raises AssertionError naming the actual status and body
response.httpx_response  # the httpx.Response, for anything else (cookies, url, history, ...)
response.websocket()  # the websockets ClientConnection, for a WebSocket response
```

HTTP-only operations on a WebSocket response, and `websocket()` on an HTTP response, raise
`InvalidResponseTypeError`. `expect_status` raises a real `AssertionError`, so it also fails under `python -O`.

## WebSockets

```python
ws = await server.client.websocket("/ws", WebSocketConfig(subprotocols=["v1"], extra_headers={"Authorization": "..."}))
helper = server.client.ws
await helper.send_text(ws, "hi")
await helper.receive_text(ws)
await helper.send_json(ws, {...})
await helper.receive_json(ws)
await helper.send_binary(ws, b"...")
await helper.receive_binary(ws)
await helper.expect_message(ws, expected={"a": 1}, timeout=1.0)  # str, bytes, or dict (compared as JSON)
messages = await helper.drain_messages(ws, timeout=0.5)  # until quiet or a normal close; needs a timeout
await ws.websocket().close()
```

- **`WebSocketConfig` fields** are all optional: `subprotocols`, `compression`, `extra_headers`, `ping_interval`,
  `ping_timeout`, `max_size`, `max_queue`, `timeout`. `None` means the default: `max_size` and `max_queue` come from
  the active `Config`, everything else from websockets. To pass a literal `None` to websockets, for example to turn
  off keepalive pings, use `options={"ping_interval": None}`.
- **Connect retries** happen only for connection errors (refused, closed before the handshake, timed out), up to
  `ws_retry_attempts` times. A rejected handshake (403, 404) or a TLS failure raises at once.
- **Closing**: `drain_messages` returns what it received when the server closes normally; an abnormal close (such
  as 1011 after the handler raised) propagates as a `websockets` exception. `expect_message` raises `TimeoutError`
  when nothing arrives.

## Config

Process-wide settings, read when a server or connection is created.

```python
from fastapi_testing import Config, set_config, get_config

set_config(
    Config(
        port_range_start=8001,
        port_range_end=9000,  # where free ports are looked for
        http_max_keepalive=20,
        http_max_connections=100,  # httpx limits for server.client
        ws_max_message_size=2**20,
        ws_max_queue_size=32,
        ws_retry_attempts=3,
        ws_retry_delay=1.0,
        shutdown="fast",  # default for servers that don't pass shutdown=
    )
)
set_config(Config.from_env())  # FASTAPI_TESTING_<SETTING>, e.g. FASTAPI_TESTING_SHUTDOWN=graceful
set_config(Config.from_env(strict=True))  # raise ValueError on unknown or invalid variables, instead of warning
set_config(Config.from_file("pyproject.toml"))  # [tool.fastapi-testing] table; .json and .toml files also work
```

Call `set_config` in `conftest.py` (at import time or in a session-scoped sync fixture). Assigning to the imported
`global_config` has no effect; use `set_config`. Invalid values raise `ValueError` when the `Config` is made.
`Config()` itself never reads the environment.

## Errors

| Error | When |
|---|---|
| `ServerStartupError` (a `RuntimeError`) | the lifespan startup raised, no port was free, or startup exceeded `startup_timeout`. Chained to the app's exception (`__cause__`). Everything acquired so far is released |
| `ServerShutdownError` (a `RuntimeError`) | the lifespan shutdown raised or exceeded `shutdown_timeout`. Raised by `stop()` after everything is released |
| `InvalidResponseTypeError` | an HTTP operation on a WebSocket response, or the reverse |
| `ValueError` | invalid `Config`, `shutdown` value, `app` together with `lifespan`, `drain_messages` without a timeout |

When the body of `async with create_test_server()` raises, that exception is what the test reports. A shutdown error
is attached to it as a note instead of replacing it, and a cancelled test stays cancelled.

## Lifecycle guarantees

`stop()` runs in a fixed order: close the client and its WebSockets, stop HTTP (fast or graceful), run the lifespan
shutdown, close the socket, release the port. If `stop()` is cancelled or a step fails, the remaining steps still
run. SIGINT and SIGTERM stop running servers as in uvicorn, and the process's original handlers come back when the
last server stops, so Ctrl-C keeps interrupting the test run.
