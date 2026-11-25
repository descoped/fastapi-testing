"""HTTP tests against the real application, with its dependencies swapped for test doubles."""

from example_app import Store

from fastapi_testing import AsyncTestServer


async def test_create_and_read_an_item(server: AsyncTestServer, store: Store):
    created = await server.client.post("/items/apple", json={"name": "Apple", "price": 1.5})
    await created.expect_status(201)
    assert await created.json() == {"id": "apple", "name": "Apple", "price": 1.5}

    # The test owns the store, so it can check state the API doesn't expose.
    assert store.items["apple"]["price"] == 1.5

    response = await server.client.get("/items/apple")
    await response.expect_status(200)
    assert (await response.json())["name"] == "Apple"


async def test_errors_are_ordinary_responses(server: AsyncTestServer):
    response = await server.client.get("/items/missing")
    await response.expect_status(404)
    assert await response.json() == {"detail": "not found"}

    await (await server.client.post("/items/a", json={"price": 1})).expect_status(201)
    await (await server.client.post("/items/a", json={"price": 1})).expect_status(409)


async def test_seeded_state_and_a_fake_upstream(server: AsyncTestServer, store: Store):
    # Arrange through the resource the test owns, instead of through the API.
    store.items["pear"] = {"name": "Pear", "price": 3.0}

    # The app calls the rates API over real HTTP; the rates_server fixture answers.
    response = await server.client.get("/items/pear/price", params={"currency": "NOK"})
    await response.expect_status(200)
    assert await response.json() == {"id": "pear", "price": 6.0, "currency": "NOK"}


async def test_upstream_failure_is_handled(server: AsyncTestServer, store: Store):
    store.items["pear"] = {"name": "Pear", "price": 3.0}
    response = await server.client.get("/items/pear/price", params={"currency": "XXX"})
    await response.expect_status(502)


async def test_routes_defined_inline(server: AsyncTestServer):
    # server.app is the FastAPI app being served; a route added during the test is live at once.
    @server.app.get("/debug/ping")
    async def ping():
        return {"pong": True}

    assert await (await server.client.get("/debug/ping")).json() == {"pong": True}
