import json
import os
import socket
from unittest.mock import patch

import pytest

import fastapi_testing.async_fastapi_testing
from fastapi_testing import create_test_server, get_config, set_config
from fastapi_testing.async_fastapi_testing import Config, PortGenerator


class TestConfig:
    """Test cases for Configuration class"""

    def test_config_defaults(self):
        """Test that Config initializes with correct defaults"""
        config = Config()
        assert config.WS_MAX_MESSAGE_SIZE == 2**20  # 1MB
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
            port_range_end=8090,
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
            "FASTAPI_TESTING_PORT_RANGE_END": "9100",
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
            "FASTAPI_TESTING_PORT_RANGE_START": "abc",
        }

        # Store original env vars
        original_env = {}
        for key in env_vars:
            original_env[key] = os.environ.get(key)
            os.environ[key] = env_vars[key]

        try:
            config = Config.from_env()
            # Should use defaults when invalid values are provided
            assert config.WS_MAX_MESSAGE_SIZE == 2**20  # Default
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
            assert config.WS_MAX_MESSAGE_SIZE == 2**20
            assert config.WS_MAX_QUEUE_SIZE == 32
            assert config.WS_RETRY_DELAY == 1.0
        finally:
            # Restore backed up env vars
            for key, value in env_backup.items():
                os.environ[key] = value

    def test_config_from_json_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {"ws_max_message_size": 4096, "ws_retry_delay": 0.5, "port_range_start": 9100, "port_range_end": 9200}
            )
        )
        config = Config.from_file(path)
        assert config.WS_MAX_MESSAGE_SIZE == 4096
        assert config.WS_RETRY_DELAY == 0.5
        assert config.PORT_RANGE_START == 9100
        assert config.HTTP_MAX_CONNECTIONS == 100  # untouched default

    def test_config_from_toml_file(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("http_max_connections = 7\nws_retry_delay = 2\n")
        config = Config.from_file(str(path))
        assert config.HTTP_MAX_CONNECTIONS == 7
        assert config.WS_RETRY_DELAY == 2  # an integer is accepted for a float setting

    def test_config_from_pyproject_tool_table(self, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "x"\n\n[tool.fastapi-testing]\nws_retry_attempts = 9\n')
        assert Config.from_file(path).WS_RETRY_ATTEMPTS == 9

    def test_config_from_pyproject_without_tool_table_uses_defaults(self, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "x"\n\n[tool.ruff]\nline-length = 120\n')
        assert Config.from_file(path).WS_RETRY_ATTEMPTS == 3

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ('{"no_such_setting": 1}', "unknown setting"),
            ('{"ws_retry_attempts": "5"}', "must be an integer"),
            ('{"ws_retry_attempts": true}', "must be an integer"),
            ('{"ws_retry_attempts": 1.5}', "must be an integer"),
            ('{"ws_retry_delay": "fast"}', "must be a number"),
            ("[1, 2]", "expected an object"),
        ],
    )
    def test_config_from_file_rejects_invalid_content(self, tmp_path, content, message):
        path = tmp_path / "config.json"
        path.write_text(content)
        with pytest.raises(ValueError, match=message):
            Config.from_file(path)

    def test_config_from_file_rejects_unsupported_type(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("ws_retry_attempts: 5\n")
        with pytest.raises(ValueError, match="Unsupported config file type"):
            Config.from_file(path)


class TestSetConfig:
    """set_config() must reach every part of the library, not only new imports."""

    @pytest.fixture(autouse=True)
    def restore_config(self):
        original = get_config()
        yield
        set_config(original)

    def test_set_config_replaces_active_config(self):
        config = Config(ws_retry_attempts=7)
        set_config(config)
        assert get_config() is config
        assert fastapi_testing.async_fastapi_testing.global_config is config

    # Pretend every port is free: this tests the generator's bookkeeping, not the machine.
    @patch.object(PortGenerator, "is_port_available", return_value=True)
    def test_default_port_generator_follows_set_config(self, _free):
        generator = PortGenerator()
        set_config(Config(port_range_start=65520, port_range_end=65522))
        assert (generator.start, generator.end) == (65520, 65522)
        assert 65520 <= generator.get_port() <= 65522

    def test_explicit_port_range_ignores_set_config(self):
        generator = PortGenerator(start=65510, end=65511)
        set_config(Config(port_range_start=65520, port_range_end=65522))
        assert (generator.start, generator.end) == (65510, 65511)

    async def test_server_uses_port_range_from_set_config(self):
        set_config(Config(port_range_start=29100, port_range_end=29120))
        async with create_test_server() as server:
            port = int(server.base_url.rsplit(":", 1)[1])
            assert 29100 <= port <= 29120


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

    # These bookkeeping tests pretend every port is free. Real fixed ports such as
    # 65530 lie in the OS ephemeral range (macOS: 49152-65535), where any process's
    # outgoing connection can hold them, which made these tests fail at random.
    @patch.object(PortGenerator, "is_port_available", return_value=True)
    def test_port_generator_exhaustion(self, _free):
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

    @patch.object(PortGenerator, "is_port_available", return_value=True)
    def test_port_generator_release_port(self, _free):
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

    def test_is_port_available_sees_a_held_port(self):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            assert not PortGenerator.is_port_available(port)
        assert PortGenerator.is_port_available(port)

    def test_get_port_skips_a_held_port(self):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            held = holder.getsockname()[1]
            generator = PortGenerator(start=held, end=held)
            with pytest.raises(RuntimeError, match="No available ports found"):
                generator.get_port()

    def test_is_port_available_agrees_with_bind_for_a_time_wait_port(self):
        """Whether a TIME_WAIT port is reusable is OS-specific (macOS: yes; Linux: only
        if the earlier socket also set SO_REUSEADDR). What must hold everywhere is
        that the availability check and the real bind agree."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with socket.socket() as client:
                client.bind(("127.0.0.1", 0))
                port = client.getsockname()[1]
                client.connect(listener.getsockname())
                accepted, _ = listener.accept()
            accepted.close()  # client closed first: its port is now in TIME_WAIT

        available = PortGenerator.is_port_available(port)
        generator = PortGenerator(start=port, end=port)
        try:
            sock = generator.bind("127.0.0.1")
        except RuntimeError:
            assert not available
        else:
            sock.close()
            generator.release_port(port)
            assert available


class TestSettingsSchema:
    """The Config constructor is the one definition of the settings."""

    def test_every_parameter_is_stored_as_its_upper_case_attribute(self):
        from fastapi_testing.async_fastapi_testing import _SETTINGS

        # Distinct values, so a parameter stored under the wrong attribute shows.
        values = {name: (index + 2) * 7 for index, name in enumerate(_SETTINGS)}
        config = Config(**values)
        assert {name: getattr(config, name.upper()) for name in _SETTINGS} == values

    def test_settings_are_integers_or_numbers(self):
        from fastapi_testing.async_fastapi_testing import _SETTINGS

        assert _SETTINGS, "no settings found in the Config signature"
        assert set(_SETTINGS.values()) <= {int, float}

    def test_env_and_file_accept_every_constructor_parameter(self, tmp_path):
        from fastapi_testing.async_fastapi_testing import _SETTINGS

        env = {f"FASTAPI_TESTING_{name.upper()}": "3" for name in _SETTINGS}
        with patch.dict("os.environ", env, clear=True):
            from_env = Config.from_env(strict=True)
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(dict.fromkeys(_SETTINGS, 3)))
        from_file = Config.from_file(path)
        for name in _SETTINGS:
            assert getattr(from_env, name.upper()) == getattr(from_file, name.upper()) == 3


class TestSettingRanges:
    """Invalid values fail when the config is made, not later as a hang or an odd error."""

    @pytest.mark.parametrize(
        ("settings", "message"),
        [
            ({"ws_retry_delay": float("inf")}, "ws_retry_delay must be a finite number"),
            ({"ws_retry_delay": -1.0}, "ws_retry_delay must be at least 0"),
            ({"ws_retry_attempts": 0}, "ws_retry_attempts must be at least 1"),
            ({"ws_max_message_size": -1}, "ws_max_message_size must be at least 1"),
            ({"http_max_connections": 0}, "http_max_connections must be at least 1"),
            ({"port_range_start": 0}, "port_range_start must be a port number from 1 to 65535"),
            ({"port_range_end": 70000}, "port_range_end must be a port number from 1 to 65535"),
            ({"port_range_start": 9100}, r"port_range_start \(9100\) is above port_range_end \(9000\)"),
        ],
    )
    def test_constructor_rejects(self, settings, message):
        with pytest.raises(ValueError, match=message):
            Config(**settings)

    def test_edge_values_are_accepted(self):
        config = Config(
            http_max_keepalive=0, ws_retry_delay=0, ws_retry_attempts=1, port_range_start=1, port_range_end=1
        )
        assert (config.HTTP_MAX_KEEPALIVE, config.WS_RETRY_DELAY, config.PORT_RANGE_END) == (0, 0, 1)

    def test_from_file_names_the_file_and_setting(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text('{"ws_retry_delay": Infinity}')
        with pytest.raises(ValueError, match=r"settings.json: 'ws_retry_delay' must be a finite number"):
            Config.from_file(path)

    def test_from_env_strict_rejects_out_of_range(self):
        env = {"FASTAPI_TESTING_WS_RETRY_DELAY": "inf", "FASTAPI_TESTING_WS_RETRY_ATTEMPTS": "0"}
        with patch.dict("os.environ", env, clear=True), pytest.raises(ValueError) as info:
            Config.from_env(strict=True)
        assert "FASTAPI_TESTING_WS_RETRY_DELAY: must be a finite number, got 'inf'" in str(info.value)
        assert "FASTAPI_TESTING_WS_RETRY_ATTEMPTS: must be at least 1, got '0'" in str(info.value)

    def test_from_env_ignores_out_of_range_with_a_warning(self, caplog):
        with patch.dict("os.environ", {"FASTAPI_TESTING_WS_RETRY_DELAY": "-2"}, clear=True):
            config = Config.from_env()
        assert config.WS_RETRY_DELAY == 1.0  # the default
        assert any("must be at least 0" in r.getMessage() for r in caplog.records)
