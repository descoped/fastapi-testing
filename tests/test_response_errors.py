import contextlib

import pytest
from fastapi import WebSocket, WebSocketDisconnect

from fastapi_testing import InvalidResponseTypeError, create_test_server


class TestAsyncTestResponseErrors:
    """Test error handling in AsyncTestResponse"""

    @pytest.mark.asyncio
    async def test_http_response_websocket_method_error(self):
        """Test calling websocket() on HTTP response raises error"""
        async with create_test_server() as server:

            @server.app.get("/test")
            async def test_endpoint():
                return {"message": "success"}

            response = await server.client.get("/test")

            with pytest.raises(InvalidResponseTypeError, match="This response is not a WebSocket connection"):
                response.websocket()

    @pytest.mark.asyncio
    async def test_websocket_response_json_error(self):
        """Test calling json() on WebSocket response raises error"""
        async with create_test_server() as server:

            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                # Keep connection open briefly
                with contextlib.suppress(WebSocketDisconnect):
                    await websocket.receive_text()

            ws_response = await server.client.websocket("/ws")

            try:
                with pytest.raises(InvalidResponseTypeError, match="Cannot get JSON directly from WebSocket response"):
                    await ws_response.json()
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_response_text_error(self):
        """Test calling text() on WebSocket response raises error"""
        async with create_test_server() as server:

            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                with contextlib.suppress(WebSocketDisconnect):
                    await websocket.receive_text()

            ws_response = await server.client.websocket("/ws")

            try:
                with pytest.raises(InvalidResponseTypeError, match="Cannot get text directly from WebSocket response"):
                    await ws_response.text()
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_response_status_code_error(self):
        """Test accessing status_code on WebSocket response raises error"""
        async with create_test_server() as server:

            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                with contextlib.suppress(WebSocketDisconnect):
                    await websocket.receive_text()

            ws_response = await server.client.websocket("/ws")

            try:
                with pytest.raises(InvalidResponseTypeError, match="WebSocket connections don't have status codes"):
                    _ = ws_response.status_code
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_response_expect_status_error(self):
        """Test calling expect_status() on WebSocket response raises error"""
        async with create_test_server() as server:

            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                with contextlib.suppress(WebSocketDisconnect):
                    await websocket.receive_text()

            ws_response = await server.client.websocket("/ws")

            try:
                with pytest.raises(InvalidResponseTypeError, match="WebSocket connections don't have status codes"):
                    await ws_response.expect_status(200)
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_http_response_normal_operations(self):
        """Test that HTTP responses work normally (for comparison)"""
        async with create_test_server() as server:

            @server.app.get("/test")
            async def test_endpoint():
                return {"message": "success"}

            response = await server.client.get("/test")

            # These should all work fine
            assert response.status_code == 200
            await response.expect_status(200)
            data = await response.json()
            assert data["message"] == "success"
            text = await response.text()
            assert "success" in text
