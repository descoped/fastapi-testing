import os

import pytest

from fastapi_testing.async_fastapi_testing import Config, PortGenerator


class TestConfig:
    """Test cases for Configuration class"""

    def test_config_defaults(self):
        """Test that Config initializes with correct defaults"""
        config = Config()
        assert config.WS_MAX_MESSAGE_SIZE == 2 ** 20  # 1MB
        assert config.WS_MAX_QUEUE_SIZE == 32
        assert config.HTTP_MAX_KEEPALIVE == 20
        assert config.HTTP_MAX_CONNECTIONS == 100
        assert config.WS_RETRY_ATTEMPTS == 3
        assert config.WS_RETRY_DELAY == 1.0
        assert config.PORT_RANGE_START == 8001
        assert config.PORT_RANGE_END == 9000

    def test_config_custom_values(self):
        """Test Config with custom values"""
        config = Config(
            ws_max_message_size=1024,
            ws_max_queue_size=16,
            http_max_keepalive=10,
            http_max_connections=50,
            ws_retry_attempts=5,
            ws_retry_delay=2.0,
            port_range_start=8080,
            port_range_end=8090
        )
        assert config.WS_MAX_MESSAGE_SIZE == 1024
        assert config.WS_MAX_QUEUE_SIZE == 16
        assert config.HTTP_MAX_KEEPALIVE == 10
        assert config.HTTP_MAX_CONNECTIONS == 50
        assert config.WS_RETRY_ATTEMPTS == 5
        assert config.WS_RETRY_DELAY == 2.0
        assert config.PORT_RANGE_START == 8080
        assert config.PORT_RANGE_END == 8090

    def test_config_from_env_with_valid_values(self):
        """Test Config.from_env() with valid environment variables"""
        # Set up environment variables
        env_vars = {
            "FASTAPI_TESTING_WS_MAX_MESSAGE_SIZE": "2097152",
            "FASTAPI_TESTING_WS_MAX_QUEUE_SIZE": "64",
            "FASTAPI_TESTING_HTTP_MAX_KEEPALIVE": "40",
            "FASTAPI_TESTING_HTTP_MAX_CONNECTIONS": "200",
            "FASTAPI_TESTING_WS_RETRY_ATTEMPTS": "5",
            "FASTAPI_TESTING_WS_RETRY_DELAY": "2.5",
            "FASTAPI_TESTING_PORT_RANGE_START": "9001",
            "FASTAPI_TESTING_PORT_RANGE_END": "9100"
        }

        # Store original env vars
        original_env = {}
        for key in env_vars:
            original_env[key] = os.environ.get(key)
            os.environ[key] = env_vars[key]

        try:
            config = Config.from_env()
            assert config.WS_MAX_MESSAGE_SIZE == 2097152
            assert config.WS_MAX_QUEUE_SIZE == 64
            assert config.HTTP_MAX_KEEPALIVE == 40
            assert config.HTTP_MAX_CONNECTIONS == 200
            assert config.WS_RETRY_ATTEMPTS == 5
            assert config.WS_RETRY_DELAY == 2.5
            assert config.PORT_RANGE_START == 9001
            assert config.PORT_RANGE_END == 9100
        finally:
            # Restore original environment
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_config_from_env_with_invalid_values(self):
        """Test Config.from_env() with invalid environment variables"""
        # Set up environment variables with invalid values
        env_vars = {
            "FASTAPI_TESTING_WS_MAX_MESSAGE_SIZE": "invalid_int",
            "FASTAPI_TESTING_WS_RETRY_DELAY": "not_a_float",
            "FASTAPI_TESTING_PORT_RANGE_START": "abc"
        }

        # Store original env vars
        original_env = {}
        for key in env_vars:
            original_env[key] = os.environ.get(key)
            os.environ[key] = env_vars[key]

        try:
            config = Config.from_env()
            # Should use defaults when invalid values are provided
            assert config.WS_MAX_MESSAGE_SIZE == 2 ** 20  # Default
            assert config.WS_RETRY_DELAY == 1.0  # Default
            assert config.PORT_RANGE_START == 8001  # Default
        finally:
            # Restore original environment
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_config_from_env_with_custom_prefix(self):
        """Test Config.from_env() with custom prefix"""
        # Set up environment variables with custom prefix
        os.environ["CUSTOM_PREFIX_WS_MAX_MESSAGE_SIZE"] = "4194304"

        try:
            config = Config.from_env(prefix="CUSTOM_PREFIX_")
            assert config.WS_MAX_MESSAGE_SIZE == 4194304
        finally:
            os.environ.pop("CUSTOM_PREFIX_WS_MAX_MESSAGE_SIZE", None)

    def test_config_from_env_no_variables(self):
        """Test Config.from_env() when no matching environment variables exist"""
        # Ensure no FASTAPI_TESTING_ variables exist
        env_backup = {}
        for key in list(os.environ.keys()):
            if key.startswith("FASTAPI_TESTING_"):
                env_backup[key] = os.environ.pop(key)

        try:
            config = Config.from_env()
            # Should use all defaults
            assert config.WS_MAX_MESSAGE_SIZE == 2 ** 20
            assert config.WS_MAX_QUEUE_SIZE == 32
            assert config.WS_RETRY_DELAY == 1.0
        finally:
            # Restore backed up env vars
            for key, value in env_backup.items():
                os.environ[key] = value

    def test_config_from_file(self):
        """Test Config.from_file() method (currently a placeholder)"""
        result = Config.from_file("dummy.json")
        assert result is None  # Current implementation just passes


class TestPortGenerator:
    """Test cases for PortGenerator edge cases"""

    def test_port_generator_no_available_ports(self):
        """Test PortGenerator when no ports are available in range"""
        # Create generator with single port range
        generator = PortGenerator(start=65535, end=65535)
        # Manually mark the only port as used
        generator.used_ports.add(65535)

        with pytest.raises(RuntimeError, match="No available ports in range"):
            generator.get_port()

    def test_port_generator_invalid_port_range(self):
        """Test PortGenerator with invalid port range"""
        generator = PortGenerator(start=65535, end=65534)  # Invalid range

        with pytest.raises(RuntimeError, match="No available ports in range"):
            generator.get_port()

    def test_is_port_available_invalid_port(self):
        """Test is_port_available with invalid port numbers"""
        # Test with port number that's too high
        assert not PortGenerator.is_port_available(999999)

        # Test with negative port
        assert not PortGenerator.is_port_available(-1)

        # Port 0 is actually a valid port that the system can bind to
        # so we'll test with a different edge case

    def test_port_generator_exhaustion(self):
        """Test PortGenerator when all ports in small range are exhausted"""
        # Use a small range for testing
        generator = PortGenerator(start=65530, end=65532)

        # Get all available ports
        port1 = generator.get_port()
        port2 = generator.get_port()
        port3 = generator.get_port()

        assert port1 in range(65530, 65533)
        assert port2 in range(65530, 65533)
        assert port3 in range(65530, 65533)
        assert len({port1, port2, port3}) == 3  # All different

        # Now all ports should be used, next call should fail
        with pytest.raises(RuntimeError, match="No available ports in range"):
            generator.get_port()

    def test_port_generator_release_port(self):
        """Test releasing and reusing ports"""
        generator = PortGenerator(start=65530, end=65530)  # Single port

        port = generator.get_port()
        assert port == 65530

        # Should fail to get another port
        with pytest.raises(RuntimeError, match="No available ports in range"):
            generator.get_port()

        # Release the port
        generator.release_port(port)

        # Should be able to get it again
        port2 = generator.get_port()
        assert port2 == 65530
