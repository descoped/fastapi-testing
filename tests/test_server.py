import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, computed_field

from fastapi_testing import create_test_server

logger = logging.getLogger(__name__)


# Models for testing
class ItemStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class Tag(BaseModel):
    name: str
    color: str
    model_config = ConfigDict(frozen=True)


class ItemModel(BaseModel):
    """Test model with various field types"""

    model_config = ConfigDict(frozen=True, validate_assignment=False)

    id: UUID = Field(default_factory=uuid4)
    name: str
    price: float
    description: str | None = None
    status: ItemStatus = ItemStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tags: Annotated[list[Tag], Field(default_factory=list)]
    metadata: Annotated[dict[str, str], Field(default_factory=dict)]

    @computed_field
    def is_active(self) -> bool:
        return self.status == ItemStatus.ACTIVE

    @computed_field
    def has_tags(self) -> bool:
        return bool(self.tags)


# Test cases using the improved test server
@pytest.mark.asyncio
async def test_basic_endpoint():
    """Test a basic GET endpoint"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Setup
        logger.info("Starting test server")
        yield
        # Cleanup
        logger.info("Shutting down test server")

    async with create_test_server(lifespan=lifespan) as server:

        @server.app.get("/test")
        async def test_endpoint():
            return {"message": "success"}

        response = await server.client.get("/test")
        await response.expect_status(200)
        data = await response.json()
        assert data["message"] == "success"


@pytest.mark.asyncio
async def test_create_item():
    """Test POST endpoint with JSON payload"""
    async with create_test_server() as server:

        @server.app.post("/items", status_code=201, response_model=ItemModel)
        async def create_item(item: ItemModel):
            return item

        test_item = {
            "name": "Test Item",
            "price": 10.99,
            "description": "A test item",
            "tags": [{"name": "test", "color": "#ff0000"}],
        }

        response = await server.client.post("/items", json=test_item)
        await response.expect_status(201)
        data = await response.json()
        assert data["name"] == test_item["name"]
        assert data["price"] == test_item["price"]


@pytest.mark.asyncio
async def test_get_items():
    """Test GET endpoint returning a list"""
    async with create_test_server() as server:

        @server.app.get("/items", response_model=list[ItemModel])
        async def get_items():
            items = [
                ItemModel(name=f"Item {i}", price=10.99 * i, tags=[Tag(name="test", color="#ff0000")]) for i in range(3)
            ]
            return items

        response = await server.client.get("/items")
        await response.expect_status(200)
        data = await response.json()
        assert len(data) == 3
        assert all(item["name"].startswith("Item") for item in data)


@pytest.mark.asyncio
async def test_update_item():
    """Test PUT endpoint"""
    async with create_test_server() as server:
        items = {}

        @server.app.put("/items/{item_id}")
        async def update_item(item_id: UUID, item: ItemModel):
            items[item_id] = item
            return item

        item_id = uuid4()
        test_item = {"name": "Updated Item", "price": 20.99, "description": "An updated item"}

        response = await server.client.put(f"/items/{item_id}", json=test_item)
        await response.expect_status(200)
        data = await response.json()
        assert data["name"] == test_item["name"]
        assert data["price"] == test_item["price"]


@pytest.mark.asyncio
async def test_delete_item():
    """Test DELETE endpoint"""
    async with create_test_server() as server:

        @server.app.delete("/items/{item_id}", status_code=204)
        async def delete_item(item_id: UUID):
            return None

        item_id = uuid4()
        response = await server.client.delete(f"/items/{item_id}")
        await response.expect_status(204)


@pytest.mark.asyncio
async def test_error_handling():
    """Test error responses"""
    async with create_test_server() as server:

        @server.app.get("/error")
        async def error_endpoint():
            raise ValueError("Test error")

        response = await server.client.get("/error")
        await response.expect_status(500)


@pytest.mark.asyncio
async def test_multiple_requests():
    """Test multiple concurrent requests"""
    async with create_test_server() as server:

        @server.app.get("/ping")
        async def ping():
            return {"status": "ok"}

        # Make multiple concurrent requests
        responses = await asyncio.gather(*[server.client.get("/ping") for _ in range(5)])

        for response in responses:
            await response.expect_status(200)
            data = await response.json()
            assert data["status"] == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
