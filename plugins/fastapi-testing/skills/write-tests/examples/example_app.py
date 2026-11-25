"""A small application built the way real projects build theirs.

It has an app factory, a settings dependency, a resource created in the lifespan
(a stand-in for a database pool), an endpoint that calls an upstream HTTP API,
a Server-Sent Events stream and a WebSocket. The tests in this folder serve it
unchanged and swap its dependencies, which is how fastapi-testing turns an
application into a simulation you control.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse


@dataclass(frozen=True)
class Settings:
    rates_url: str = "https://rates.example.com"


def get_settings() -> Settings:
    return Settings()


class Store:
    """Stands in for a database pool or repository that must be opened and closed."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def get_store(request: Request) -> Store:
    # Production reads the store the lifespan created; tests override this dependency.
    store: Store = request.state.store
    return store


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[dict[str, Any]]:
        store = Store()
        try:
            yield {"store": store}  # lifespan state: reaches request.state.store
        finally:
            await store.close()

    app = FastAPI(lifespan=lifespan)

    @app.post("/items/{item_id}", status_code=201)
    async def create_item(item_id: str, item: dict[str, Any], store: Store = Depends(get_store)):
        if item_id in store.items:
            raise HTTPException(status_code=409, detail="exists")
        store.items[item_id] = item
        return {"id": item_id, **item}

    @app.get("/items/{item_id}")
    async def read_item(item_id: str, store: Store = Depends(get_store)):
        if item_id not in store.items:
            raise HTTPException(status_code=404, detail="not found")
        return {"id": item_id, **store.items[item_id]}

    @app.get("/items/{item_id}/price")
    async def price(
        item_id: str,
        currency: str = "EUR",
        store: Store = Depends(get_store),
        settings: Settings = Depends(get_settings),
    ):
        item = store.items.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        async with httpx.AsyncClient(base_url=settings.rates_url) as upstream:
            response = await upstream.get(f"/rates/{currency}")
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="rates unavailable")
        return {"id": item_id, "price": round(item["price"] * response.json()["rate"], 2), "currency": currency}

    @app.get("/events")
    async def events(count: int = 3):
        async def stream() -> AsyncIterator[str]:
            for n in range(count):
                yield f"data: tick {n}\n\n"
                await asyncio.sleep(0.01)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket("/ws/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json({"echo": await websocket.receive_json()})
        except WebSocketDisconnect:
            return

    return app
