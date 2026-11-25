# Fixtures for fastapi-testing

## Contents
- [Why scope matters](#why-scope-matters)
- [The standard server fixture](#the-standard-server-fixture)
- [Resources the server uses](#resources-the-server-uses)
- [What the app's own lifespan opens](#what-the-apps-own-lifespan-opens)
- [What may be session-scoped](#what-may-be-session-scoped)
- [Sharing one server across tests](#sharing-one-server-across-tests)
- [Factory fixtures](#factory-fixtures)
- [Global state in the app](#global-state-in-the-app)

## Why scope matters

With `asyncio_default_fixture_loop_scope = "function"` (pytest-asyncio's default), every test runs on its own event
loop, and the loop is closed when the test ends. An asyncio object (a connection pool, an httpx client, a database
engine, a task) belongs to the loop it was created on. Two failures follow from that:

- **Reused across loops**: an object created in test A's loop and used in test B raises "attached to a different
  loop" or hangs.
- **Still busy when its loop closes**: a pool still opening connections, or a background task still running, keeps
  the loop's shutdown waiting. The suite hangs after a test that passed, or fails at teardown. A slow server stop
  can hide this race; a fast one exposes it.

So: create every loop-bound object in a function-scoped fixture, and close it in the same fixture.

## The standard server fixture

```python
@pytest.fixture
async def server(db_pool, fake_mailer) -> AsyncGenerator[AsyncTestServer]:
    app = create_app()  # the project's own factory
    app.dependency_overrides[get_db_pool] = lambda: db_pool
    app.dependency_overrides[get_mailer] = lambda: fake_mailer
    async with create_test_server(app=app) as server:
        yield server
```

- **Teardown order**: because `server` takes `db_pool` as a parameter, pytest stops the server before it closes the
  pool. Requests can't run against a closed pool.
- **Overrides go on the app, before the server starts.** They apply to HTTP and WebSocket routes. Keys are the
  dependency callables the routes use (`Depends(get_db_pool)`), so override the function the route names, not a
  function it calls inside.
- **A project whose app reads globals at import time** (a module-level `app = FastAPI()` that reads settings when
  imported) still works: import `app`, set overrides, pass `app=app`, and clear `app.dependency_overrides` in the
  fixture's `finally`, because that app object is shared by every test.

## Resources the server uses

Every resource fixture follows the same shape: open in the fixture, fully ready before it yields, closed in
`finally`.

**psycopg 3 pool**

```python
@pytest.fixture
async def db_pool(database_url: str) -> AsyncGenerator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(database_url, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)  # don't yield while connections are still being opened
    try:
        yield pool
    finally:
        await pool.close()
```

`open()` without `wait=True` returns at once and fills the pool in the background. A quick test then ends, and its
loop closes, while those connections are still being opened.

**SQLAlchemy async engine**

```python
@pytest.fixture
async def engine(database_url: str) -> AsyncGenerator[AsyncEngine]:
    engine = create_async_engine(database_url)
    try:
        yield engine
    finally:
        await engine.dispose()
```

**Keeping tests independent in a shared database**: give each test clean tables. Truncate in the fixture's teardown,
or create a schema per test. Wrapping each test in a transaction that is rolled back also works when the app takes
its connection from an overridden dependency, but then every request in the test shares that one connection, so it
doesn't fit tests that send concurrent requests.

**Fakes for outbound services**: an in-memory fake (a class with the same methods) injected through
`dependency_overrides`, or a second `create_test_server()` with inline routes when the app's HTTP client code should
run for real (see `strategies.md`). Fakes that the test inspects afterwards are fixtures too, so the test can take
them as parameters.

## What the app's own lifespan opens

`create_test_server(app=create_app())` runs the app's real lifespan. `dependency_overrides` don't reach it, because
a lifespan is not a dependency. So a lifespan that opens a pool from `DATABASE_URL`, connects a client, or starts a
consumer does that in every test, against whatever its settings point at. Read the lifespan before writing the
fixture, and decide for each resource it opens:

- **Point it at test infrastructure.** Most lifespans read settings from the environment. When settings are read
  when the lifespan runs, set the variable in the fixture with `monkeypatch.setenv("DATABASE_URL", test_url)`
  before the server starts. When they are read at import time (a module-level `settings = Settings()`, or a
  dataclass default computed from `os.environ`), set it at the top of `conftest.py`, before importing the app:
  `os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)`. If the factory takes settings
  (`create_app(settings)`), pass test settings instead.
- **Use what the lifespan created** instead of a second copy, when the app exposes it (`app.state.pool`, lifespan
  state). The test then reads and seeds through the same pool the app uses.
- **Keep it from connecting at all** when the resource is external and slow (a message broker, a cloud client):
  give the factory a flag or setting that skips it, and override the dependency that hands it to routes.

Never leave a test suite that reaches the production URL a setting defaults to. A test that passes only because
the developer's machine has a database at that default fails in CI, or worse, writes to a real one.

## What may be session-scoped

Session scope is for things that are not bound to an event loop:

- settings objects, database URLs, test data files;
- a database container (testcontainers) or a server process, started with a sync API;
- migrations or schema creation run once, with a sync driver, or inside their own `asyncio.run()` that closes every
  connection before it returns.

Never keep an async pool, engine, client or task on a session-scoped fixture, even one created lazily "in the first
test's loop". The next test runs on a different loop.

## Sharing one server across tests

A server per test costs about 6 ms with the default fast shutdown, so per-test servers are the default choice. Share
one only when startup is genuinely expensive (a lifespan that loads a model, for example), and then do it
deliberately with a matching loop scope:

```python
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared_server() -> AsyncGenerator[AsyncTestServer]:
    async with create_test_server(app=create_app()) as server:
        yield server


@pytest.mark.asyncio(loop_scope="module")
async def test_reads(shared_server: AsyncTestServer): ...
```

Every test using it must run on the module loop, and every resource it uses must be module-scoped as well. State
written by one test is visible to the next, so keep such tests read-only or reset state explicitly.

## Factory fixtures

When tests need servers with different overrides or options, yield a factory, and let the fixture stop whatever it
started:

```python
@pytest.fixture
async def make_server() -> AsyncGenerator[Callable[..., Awaitable[AsyncTestServer]]]:
    servers: list[AsyncTestServer] = []

    async def make(overrides: Mapping[Callable[..., Any], Callable[..., Any]] | None = None) -> AsyncTestServer:
        app = create_app()
        app.dependency_overrides.update(overrides or {})  # keys are the dependency functions themselves
        server = AsyncTestServer(app=app)
        await server.start()
        servers.append(server)
        return server

    try:
        yield make
    finally:
        for server in reversed(servers):
            await server.stop()


async def test_with_a_failing_mailer(make_server):
    server = await make_server({get_mailer: lambda: FailingMailer()})
```

## Global state in the app

Apps that keep a module-level container, registry or cache (`set_container(...)`, a global client) carry state from
one test to the next. Reset or replace it in the server fixture and restore it in `finally`. Prefer passing such
objects through `app.state`, lifespan state or dependencies, which `dependency_overrides` can replace per test.
