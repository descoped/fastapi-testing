# Upgrading fastapi-testing

Check the installed version first (`uv pip show fastapi-testing`). The changes below are what breaks or behaves
differently; each section lists what to change in a test suite.

## 0.4 → 0.5: fast shutdown by default

- **`stop()` is fast.** Open connections (keep-alive, SSE, WebSocket) are closed at once, which the app sees as a
  client disconnect. Requests still running get 0.5 s, then are cancelled without logging an error. The app's
  lifespan shutdown still runs. A stop takes about a millisecond instead of about 200 ms.
- **What can break**:
  - a test that relied on an in-flight request finishing during `stop()`: pass `shutdown="graceful"` for that test;
  - fixtures that leave a loop-bound resource busy at the end of the test (a pool still opening connections): the
    slow stop used to hide this, and now the run can hang at loop teardown. Fix the fixture (see the troubleshoot
    skill); don't switch the whole suite to graceful to hide it.
- **New**: `shutdown="fast" | "graceful"` on `AsyncTestServer`, `create_test_server` and `Config`
  (`FASTAPI_TESTING_SHUTDOWN` through `Config.from_env()`), and the `ShutdownMode` type.
- **Fixed**: a SIGINT or SIGTERM received just before `stop()` is no longer lost.

## 0.3 → 0.4: the server owns its lifecycle

- **Startup errors raise `ServerStartupError`**, chained to the app's exception (`__cause__`). It no longer carries
  a uvicorn exit code and no longer arrives as `SystemExit`. Replace `pytest.raises(SystemExit)` with
  `pytest.raises(ServerStartupError)`.
- **Shutdown errors raise `ServerShutdownError`** from `stop()` after everything is released. uvicorn used to only
  log them, so a suite whose app has a broken lifespan shutdown starts failing; fix the lifespan.
- **The test's own error wins.** When the body of `async with create_test_server()` raises, that exception
  propagates, with a shutdown error attached as a note.
- **Client timeout is separate**: `client_timeout` (default 30 s) no longer follows `startup_timeout`. If you raised
  `startup_timeout` to allow long requests, pass `client_timeout=` as well.
- **No HTTP/2 extra**: the dependency is plain `httpx`. If your own code needs HTTP/2, depend on `httpx[http2]`.
- **`drain_messages()`** returns what arrived when the server closes normally; an abnormal close still raises.
  `timeout=None` raises `ValueError` instead of waiting forever.
- **`expect_status()`** raises `AssertionError` explicitly (also under `python -O`), with the response body in the
  message.
- **WebSocket connects** retry only connection errors. A rejected handshake (403/404) or a TLS failure fails at once.
- **Config is validated**: sizes, limits and attempts at least 1 (`http_max_keepalive` at least 0), `ws_retry_delay`
  finite and at least 0, ports 1–65535 with start ≤ end; otherwise `ValueError`. `Config.from_env()` warns about
  unknown or invalid `FASTAPI_TESTING_*` variables; `strict=True` raises.
- **Typing**: the package ships `py.typed`. A lifespan written as a bare async generator function is now a type
  error; decorate it with `@asynccontextmanager`.
- **Other**: `WebSocketConfig().max_size` and `.max_queue` default to `None` (falling back to `Config`);
  `PortGenerator.start` and `.end` are read-only.
- **New**: `create_test_server(app=...)` / `AsyncTestServer(app=...)`, `client_timeout`, `AsyncTestClient.stream()`,
  `AsyncTestResponse.httpx_response`, `get_config()` / `set_config()`, `Config.from_file()`, and a shared
  `server.client` that `async with server.client` no longer closes (`stop()` does).
- **Fixed**: Ctrl-C and SIGTERM keep working when overlapping servers stop in any order; the listening socket is
  always closed; no check-then-bind port race; `stop()` after a failed `start()` no longer leaks.

## Upgrading a suite

1. Bump the dependency and run the suite with `-W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning`.
2. Fix failures from the lists above, starting with fixtures that hang.
3. Replace hand-written lifespan wiring with `create_test_server(app=create_app())` where the suite rebuilt the app's
   lifespan in a custom `lifespan=` only to install overrides; set `app.dependency_overrides` instead.
4. Compare total run time before and after; with 0.5 it should drop by about 200 ms per server.
