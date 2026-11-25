# fastapi-testing in Jupyter notebooks

A notebook is a quick way to explore an API before writing the pytest file. Jupyter's IPython kernel runs an event
loop and allows top-level `await`, so the library works without `asyncio.run()`. Verified with ipykernel on Python
3.13.

## One cell

```python
from fastapi_testing import create_test_server
from myproject.main import create_app

async with create_test_server(app=create_app()) as server:
    response = await server.client.get("/health")
    print(response.status_code, await response.json())
```

## A server kept across cells

```python
# cell 1
from fastapi_testing import AsyncTestServer

server = AsyncTestServer(app=create_app())
await server.start()

# cell 2 ... n: explore
response = await server.client.post("/items/a", json={"price": 1})
await response.json()

# last cell: always stop it, which frees the port and closes connections
await server.stop()
```

- **Stop before you re-run the start cell.** `start()` on a running server raises `RuntimeError`, and creating a
  new server without stopping the old one leaves the old port, connections and lifespan open until the kernel
  restarts.
- **Interrupting the kernel** (the stop button) works normally. ipykernel installs its own SIGINT handler for each
  cell and puts the default back afterwards, so a server left running between cells doesn't take over Ctrl-C.
- **Restarting the kernel** releases everything.

## From notebook to test

When the exploration works, move it into a pytest file: the server becomes the fixture from `fixtures.md`, each
exploration cell becomes a test, and `print` becomes `assert`. Keep the notebook only as scratch; the tests are what
CI runs.
