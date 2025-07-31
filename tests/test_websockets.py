import json
import logging

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from fastapi_testing import create_test_server
from fastapi_testing.async_fastapi_testing import WebSocketConfig

logger = logging.getLogger(__name__)


@pytest.fixture
async def test_server():
    async with create_test_server() as server:

        @server.app.get("/api/data")
        async def get_data():
            return {"status": "ok"}

        @server.app.websocket("/ws/echo")
        async def websocket_endpoint(websocket: WebSocket):
            await echo_handler(websocket)

        yield server


async def echo_handler(websocket: WebSocket):
    try:
        await websocket.accept(subprotocol="test-protocol")
        while True:
            try:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return

                if "text" in message:
                    try:
                        data = json.loads(message["text"])
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON decode error: {e}")
                        # Gracefully close the connection with code 1003 (Unsupported Data)
                        await websocket.close(code=1003)
                        return
                    await websocket.send_json(data)
                elif "bytes" in message:
                    await websocket.send_bytes(message["bytes"])
            except WebSocketDisconnect:
                logger.info("WebSocket disconnected")
                return
            except Exception as e:
                logger.error(f"Error handling message: {e}")
                raise
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
        raise


@pytest.mark.asyncio
async def test_mixed_protocols(test_server):
    http_response = await test_server.client.get("/api/data")
    assert http_response.status_code == 200

    config = WebSocketConfig(subprotocols=["test-protocol"], ping_interval=20.0, ping_timeout=20.0)

    ws_response = await test_server.client.websocket("/ws/echo", config)

    try:
        test_json = {"message": "test"}
        await test_server.client.ws.send_json(ws_response, test_json)
        response = await test_server.client.ws.receive_json(ws_response)
        assert response == test_json

        test_data = b"binary test"
        await test_server.client.ws.send_binary(ws_response, test_data)
        response = await test_server.client.ws.receive_binary(ws_response)
        assert response == test_data
    finally:
        await ws_response.websocket().close()


@pytest.mark.asyncio
async def test_invalid_websocket_json(test_server):
    """Test that sending invalid JSON over WebSocket gracefully closes the connection."""
    config = WebSocketConfig(subprotocols=["test-protocol"])
    ws_response = await test_server.client.websocket("/ws/echo", config)
    try:
        # Send a non-JSON string using the helper.
        await test_server.client.ws.send_text(ws_response, "invalid json")
        with pytest.raises(ConnectionClosedError):
            # The server should close the connection upon receiving invalid JSON.
            await test_server.client.ws.receive_json(ws_response)
    finally:
        # Ensure the connection is closed if still open.
        if ws_response.websocket().state != State.CLOSED:
            await ws_response.websocket().close()
