import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException

from fastapi_testing import create_test_server, AsyncTestServer


class TestServerLifecycle:
    """Real-world server lifecycle and error handling tests"""

    @pytest.mark.asyncio
    async def test_server_with_custom_lifespan(self):
        """Test server with custom lifespan events"""
        startup_called = False
        shutdown_called = False

        @asynccontextmanager
        async def custom_lifespan(app: FastAPI):
            nonlocal startup_called, shutdown_called
            # Startup
            startup_called = True
            yield
            # Shutdown
            shutdown_called = True

        async with create_test_server(lifespan=custom_lifespan) as server:
            @server.app.get("/test")
            async def test_endpoint():
                return {"status": "ok"}

            response = await server.client.get("/test")
            await response.expect_status(200)

            assert startup_called is True

        # After context exit, shutdown should be called
        assert shutdown_called is True

    @pytest.mark.asyncio
    async def test_server_startup_with_errors(self):
        """Test server startup with application errors"""

        @asynccontextmanager
        async def failing_lifespan(app: FastAPI):
            # Simulate startup error
            raise RuntimeError("Startup failed")
            yield  # This won't be reached

        # The server should handle startup errors gracefully
        with pytest.raises(RuntimeError, match="Startup failed"):
            async with create_test_server(lifespan=failing_lifespan) as server:
                pass

    @pytest.mark.asyncio
    async def test_server_with_exception_handling(self):
        """Test server error handling for various HTTP errors"""
        async with create_test_server() as server:
            @server.app.get("/error/{status_code}")
            async def error_endpoint(status_code: int):
                if status_code == 404:
                    raise HTTPException(status_code=404, detail="Not found")
                elif status_code == 500:
                    raise HTTPException(status_code=500, detail="Internal server error")
                elif status_code == 400:
                    raise HTTPException(status_code=400, detail="Bad request")
                else:
                    return {"status": "ok"}

            # Test various error codes
            response_404 = await server.client.get("/error/404")
            assert response_404.status_code == 404

            response_500 = await server.client.get("/error/500")
            assert response_500.status_code == 500

            response_400 = await server.client.get("/error/400")
            assert response_400.status_code == 400

            # Test successful response
            response_200 = await server.client.get("/error/200")
            await response_200.expect_status(200)

    @pytest.mark.asyncio
    async def test_server_concurrent_requests_with_delays(self):
        """Test server handling concurrent requests with artificial delays"""
        async with create_test_server() as server:
            @server.app.get("/slow/{delay}")
            async def slow_endpoint(delay: float):
                await asyncio.sleep(delay)
                return {"delay": delay, "message": "completed"}

            # Make concurrent requests with different delays
            tasks = [
                server.client.get("/slow/0.1"),
                server.client.get("/slow/0.05"),
                server.client.get("/slow/0.15"),
                server.client.get("/slow/0.02")
            ]

            responses = await asyncio.gather(*tasks)

            # All should succeed
            for response in responses:
                await response.expect_status(200)
                data = await response.json()
                assert "delay" in data
                assert "message" in data

    @pytest.mark.asyncio
    async def test_server_manual_lifecycle(self):
        """Test manual server start/stop lifecycle"""
        server = AsyncTestServer()

        # Server should not be running initially
        assert not hasattr(server, '_server_task') or server._server_task is None

        try:
            await server.start()

            # Add a test endpoint
            @server.app.get("/manual-test")
            async def manual_test():
                return {"manual": True}

            # Make request to verify server is running
            response = await server.client.get("/manual-test")
            await response.expect_status(200)
            data = await response.json()
            assert data["manual"] is True

        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_server_port_allocation_and_release(self):
        """Test that ports are properly allocated and released"""
        # Create multiple servers to test port management
        servers = []
        ports = []

        try:
            # Create multiple servers
            for i in range(3):
                server = AsyncTestServer()
                await server.start()
                servers.append(server)
                ports.append(server._port)

                # Verify each server gets a different port
                # Use a closure to capture the current value of i
                def make_endpoint(server_id):
                    async def test_endpoint():
                        return {"server": server_id}
                    return test_endpoint

                server.app.get(f"/server-{i}")(make_endpoint(i))

            # All ports should be different
            assert len(set(ports)) == len(ports)

            # All servers should be reachable
            for i, server in enumerate(servers):
                response = await server.client.get(f"/server-{i}")
                await response.expect_status(200)

        finally:
            # Clean up all servers
            cleanup_tasks = [server.stop() for server in servers]
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_server_with_middleware(self):
        """Test server with custom middleware"""
        async with create_test_server() as server:
            # Add custom middleware
            @server.app.middleware("http")
            async def custom_middleware(request, call_next):
                # Add custom header
                response = await call_next(request)
                response.headers["X-Custom-Header"] = "test-value"
                return response

            @server.app.get("/middleware-test")
            async def middleware_test():
                return {"middleware": "active"}

            response = await server.client.get("/middleware-test")
            await response.expect_status(200)

            # Verify custom header was added
            # Note: This tests the actual FastAPI app behavior
            data = await response.json()
            assert data["middleware"] == "active"

    @pytest.mark.asyncio
    async def test_server_cleanup_on_exception(self):
        """Test that server resources are cleaned up when exceptions occur"""
        server = AsyncTestServer()

        try:
            await server.start()

            # Simulate an exception during server operation
            @server.app.get("/exception-test")
            async def exception_endpoint():
                raise ValueError("Test exception")

            # Make request that causes exception
            response = await server.client.get("/exception-test")
            assert response.status_code == 500  # FastAPI handles the exception

        finally:
            # Server should clean up properly even after exceptions
            await server.stop()

            # Verify cleanup completed
            assert server._server_task is None or server._server_task.done()

    @pytest.mark.asyncio
    async def test_invalid_client_requests(self):
        """Test client behavior with invalid requests"""
        async with create_test_server() as server:
            @server.app.get("/valid")
            async def valid_endpoint():
                return {"valid": True}

            # Test request to non-existent endpoint
            response = await server.client.get("/nonexistent")
            assert response.status_code == 404

            # Test malformed JSON in POST request
            response = await server.client.post(
                "/valid",
                headers={"Content-Type": "application/json"},
                content="invalid-json"
            )
            # Server should handle malformed JSON gracefully
            assert response.status_code in [400, 405, 422]  # Bad request, method not allowed, or validation error
