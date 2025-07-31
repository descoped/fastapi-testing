import asyncio

import pytest
from fastapi import WebSocket, WebSocketDisconnect

from fastapi_testing import create_test_server, WebSocketConfig


class TestWebSocketRealWorld:
    """Real-world WebSocket testing scenarios"""

    @pytest.mark.asyncio
    async def test_websocket_type_errors_real(self):
        """Test WebSocket helper methods with actual mismatched data types"""
        async with create_test_server() as server:
            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                try:
                    while True:
                        # Send binary data when client expects text/JSON
                        await websocket.send_bytes(b"binary_response")
                        await asyncio.sleep(0.1)
                except WebSocketDisconnect:
                    pass

            ws_response = await server.client.websocket("/ws")

            try:
                # Try to receive JSON but server sends binary - should raise TypeError
                with pytest.raises(TypeError, match="Expected text data to decode JSON, got <class 'bytes'>"):
                    await server.client.ws.receive_json(ws_response)
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_receive_text_from_binary(self):
        """Test receiving text when binary data is sent"""
        async with create_test_server() as server:
            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                try:
                    # Send binary data
                    await websocket.send_bytes(b"binary_data")
                    await websocket.receive()  # Wait for client
                except WebSocketDisconnect:
                    pass

            ws_response = await server.client.websocket("/ws")

            try:
                # Try to receive text but server sends binary
                with pytest.raises(TypeError, match="Expected str, got <class 'bytes'>"):
                    await server.client.ws.receive_text(ws_response)
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_receive_binary_from_text(self):
        """Test receiving binary when text data is sent"""
        async with create_test_server() as server:
            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                try:
                    # Send text data
                    await websocket.send_text("text_data")
                    await websocket.receive()  # Wait for client
                except WebSocketDisconnect:
                    pass

            ws_response = await server.client.websocket("/ws")

            try:
                # Try to receive binary but server sends text
                with pytest.raises(TypeError, match="Expected bytes, got <class 'str'>"):
                    await server.client.ws.receive_binary(ws_response)
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_expect_message_timeout(self):
        """Test expect_message with real timeout scenario"""
        async with create_test_server() as server:
            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                try:
                    # Wait for client message
                    await websocket.receive_text()
                    # Delay response longer than timeout
                    await asyncio.sleep(0.2)
                    await websocket.send_text("delayed_response")
                except WebSocketDisconnect:
                    pass

            ws_response = await server.client.websocket("/ws")

            try:
                # Send a message first
                await server.client.ws.send_text(ws_response, "trigger")

                # Expect a specific message but timeout before it arrives
                with pytest.raises(TimeoutError, match="Timeout waiting for expected message"):
                    await server.client.ws.expect_message(
                        ws_response,
                        expected="expected_message",
                        timeout=0.1
                    )
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_drain_messages_timeout(self):
        """Test drain_messages with timeout"""
        async with create_test_server() as server:
            @server.app.websocket("/ws")
            async def ws_endpoint(websocket: WebSocket):
                await websocket.accept()
                try:
                    # Send a few quick messages
                    await websocket.send_text("message1")
                    await websocket.send_text("message2")
                    # Then wait longer than timeout
                    await asyncio.sleep(0.2)
                    await websocket.send_text("delayed_message")
                except WebSocketDisconnect:
                    pass

            ws_response = await server.client.websocket("/ws")

            try:
                # Drain messages with short timeout
                messages = await server.client.ws.drain_messages(
                    ws_response,
                    timeout=0.1
                )

                # Should get the first two messages before timeout
                assert len(messages) == 2
                assert messages[0] == "message1"
                assert messages[1] == "message2"
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_config_with_custom_settings(self):
        """Test WebSocket connection with custom configuration"""
        async with create_test_server() as server:
            @server.app.websocket("/ws")
            async def ws_endpoint(ws: WebSocket):
                await ws.accept(subprotocol="test-protocol")
                try:
                    message = await ws.receive_text()
                    await ws.send_text(f"echo: {message}")
                except WebSocketDisconnect:
                    pass

            config = WebSocketConfig(
                subprotocols=["test-protocol"],
                extra_headers={"X-Test-Header": "test-value"},
                ping_interval=30.0,
                ping_timeout=10.0,
                timeout=5.0,
                max_size=1024 * 1024,  # 1MB
                max_queue=16
            )

            ws_response = await server.client.websocket("/ws", config=config)

            try:
                # Test that the connection works with custom config
                await server.client.ws.send_text(ws_response, "test_message")
                response = await server.client.ws.receive_text(ws_response)
                assert response == "echo: test_message"

                # Verify the subprotocol was selected
                websocket = ws_response.websocket()
                assert websocket.subprotocol == "test-protocol"
            finally:
                await ws_response.websocket().close()

    @pytest.mark.asyncio
    async def test_websocket_connection_failure_scenarios(self):
        """Test WebSocket connection failure and retry scenarios"""
        # Test connection to invalid endpoint
        async with create_test_server() as server:
            # Don't define any WebSocket endpoint

            # This should fail to connect
            with pytest.raises(Exception):  # Connection will fail
                await server.client.websocket("/nonexistent")

    @pytest.mark.asyncio
    async def test_multiple_websocket_connections(self):
        """Test handling multiple WebSocket connections simultaneously"""
        async with create_test_server() as server:
            @server.app.websocket("/ws/{client_id}")
            async def ws_endpoint(websocket: WebSocket, client_id: str):
                await websocket.accept()
                try:
                    while True:
                        message = await websocket.receive_text()
                        await websocket.send_text(f"{client_id}: {message}")
                except WebSocketDisconnect:
                    pass

            # Create multiple connections
            ws1 = await server.client.websocket("/ws/client1")
            ws2 = await server.client.websocket("/ws/client2")

            try:
                # Send messages concurrently
                await asyncio.gather(
                    server.client.ws.send_text(ws1, "hello1"),
                    server.client.ws.send_text(ws2, "hello2")
                )

                # Receive responses
                response1 = await server.client.ws.receive_text(ws1)
                response2 = await server.client.ws.receive_text(ws2)

                assert response1 == "client1: hello1"
                assert response2 == "client2: hello2"
            finally:
                await ws1.websocket().close()
                await ws2.websocket().close()
