---
name: troubleshoot
description: Diagnose and fix failing, hanging or leaking test suites that use fastapi-testing (AsyncTestServer, create_test_server). Use it when pytest hangs after a test passes or at teardown, a run never finishes, tests fail with "attached to a different loop" or "Event loop is closed", ResourceWarning or PytestUnraisableExceptionWarning appear, "Exception in ASGI application" is logged, a request or stream is cut off when the server stops, ServerStartupError or ServerShutdownError is raised, ports run out, Ctrl-C stops working, tests pass only with a sleep, or the suite broke or changed speed after upgrading fastapi-testing (0.3 → 0.4 → 0.5).
---

# Troubleshooting fastapi-testing suites

Start from the symptom. Most problems in async test suites come from one cause: an object that belongs to one
test's event loop outlives that test. fastapi-testing itself releases its port, socket, tasks, connections and
signal handlers on every path, so look first at the fixtures around it.

Check the installed version before anything else (`uv pip show fastapi-testing`, or the lock file). Behaviour
changed in 0.4 (lifecycle and errors) and 0.5 (fast shutdown by default): `references/migration.md`.

## Symptoms

| Symptom | Likely cause | Fix |
|---|---|---|
| The run hangs after a test passes, or at teardown; a stack dump ends in `Runner.close()` / `_cancel_all_tasks` | A resource is still busy when the test's loop closes: a DB pool still opening connections, a background task, an unclosed client | Find the task (below). Open pools fully before yielding (`await pool.open(wait=True)`), close every resource in its own fixture, make the server fixture depend on its resources |
| "attached to a different loop", "Event loop is closed", "Future ... attached to a different loop" | An async object created in one test's loop is reused in another, usually through a session- or module-scoped fixture | Make that fixture function-scoped. Session scope only for things that aren't loop-bound (settings, URLs, containers started synchronously) |
| The hang appeared after upgrading to 0.5 | 0.5 stops in about a millisecond instead of 200 ms; the slow stop was hiding the race above | Fix the fixture as above. `shutdown="graceful"` only hides it again |
| Passes only with `asyncio.sleep(...)` added | The same race, or a test asserting before background work finished | Remove the sleep; fix ownership, or wait for the effect with a polling helper |
| `ResourceWarning: unclosed <socket>` or transport | A client or stream the test or app opened and never closed: your own `AsyncTestClient`/`httpx.AsyncClient` without `async with`, `client.stream()` without `async with`, a client the app creates per request | Use `async with` for every client and stream; close app-level clients in the app's lifespan |
| `PytestUnraisableExceptionWarning` | An exception in a destructor or callback after the test: a resource closed on the wrong loop, or garbage-collected while open | Same as the two rows above; run with `-W error::pytest.PytestUnraisableExceptionWarning` to fail at the source |
| A request fails with a transport error or is cancelled when the server stops | 0.5's fast stop: running requests get 0.5 s (`request_grace`), then are cancelled | Await the request before leaving the `async with` block, or use `shutdown="graceful"` for a test about shutdown |
| Cleanup in an SSE generator's `finally` doesn't finish | Starlette cancels a streaming response on disconnect, in production too, so cleanup stops at its first `await` | Shield the cleanup in the app: `with anyio.CancelScope(shield=True): ...` |
| "Exception in ASGI application" in the log | A handler raised. (In 0.5, cancellation of requests during a fast stop is not logged) | Read the traceback; it is a real application error |
| "N request(s) ignored cancellation at stop; leaving them" | A handler catches `CancelledError` (or `BaseException`) and keeps running | Let `CancelledError` propagate in the handler |
| `ServerStartupError` | The app's lifespan startup raised, startup took longer than `startup_timeout`, or no port was free | Read `exc.__cause__`, the app's own error. Raise `startup_timeout` for slow startups. For ports, see below |
| Tests need a production `DATABASE_URL` (or other service) to start, or connect somewhere they shouldn't | `app=create_app()` runs the app's real lifespan, which opens its resources from its settings; `dependency_overrides` don't reach the lifespan | Point those settings at test infrastructure before the server starts (`monkeypatch.setenv`, or `os.environ` at the top of `conftest.py` when settings are read at import), or use what the lifespan created. See `write-tests` → `references/fixtures.md` |
| `ModuleNotFoundError: No module named 'app'` under `uv run pytest` | The project isn't installed and the tests import its package | Add `pythonpath = ["."]` under `[tool.pytest.ini_options]` |
| `ServerShutdownError` from `stop()` | The app's lifespan shutdown raised or exceeded `shutdown_timeout` | Fix the lifespan cleanup; the error names it. Everything else was already released |
| "No available ports in range" | The port range is too small for the number of servers running at once | Widen it: `set_config(Config(port_range_start=..., port_range_end=...))`. Ports are bound atomically, so parallel processes (pytest-xdist) don't collide |
| Ctrl-C or SIGTERM ignored after some tests | fastapi-testing older than 0.4, where overlapping servers left a dead handler installed | Upgrade to 0.4 or later |
| A WebSocket connect takes seconds, then fails | Nothing is listening at that URL yet (connection refused is retried `ws_retry_attempts` times) | Make sure the server is started and the path is right. A 403/404 handshake fails at once and is not retried |
| The suite got slower after upgrading to 0.4 | 0.4 waited in uvicorn's graceful drain (about 200 ms per stop) | Upgrade to 0.5, where stop is fast by default |
| Type checker rejects `lifespan=` | A bare async generator function; Starlette's type requires a context manager | Decorate it with `@asynccontextmanager` |

## Find what is still running

Add this to `conftest.py` while you investigate. It fails the test that leaves a task behind and names the task.
Autouse fixtures are torn down last, so it runs after the server and resource fixtures have closed:

```python
@pytest.fixture(autouse=True)
async def report_pending_tasks():
    yield
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert not pending, f"tasks still running after the test: {[t.get_name() for t in pending]}"
```

For a hang with no failure, get a stack dump instead: set `faulthandler_timeout = 60` under
`[tool.pytest.ini_options]`, or run one file with `-x -vv` and press Ctrl-C to see where it waits. Task names such as
`pool-1-worker-0` point at psycopg_pool; an `httpx` or `anyio` frame points at an unclosed client.

Then make warnings fail where they happen:

```bash
pytest -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning
PYTHONTRACEMALLOC=20 pytest ...      # ResourceWarnings then show where the object was created
```

## The fix, in general

1. Every loop-bound resource (pool, engine, client, task) is created in a function-scoped fixture and is ready
   before the fixture yields.
2. The fixture that opens it closes it, in `finally`.
3. The server fixture takes those resources as parameters, so pytest stops the server first.
4. No sleeps; wait for effects with a polling helper and a timeout.

Patterns for each of these: the `write-tests` skill, `references/fixtures.md`.

## Report back

When you change a suite, run it twice with the warning flags above and report the pass count, the total time, and
whether any warning or pending task remains.
