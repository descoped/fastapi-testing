from typing import TYPE_CHECKING

from fastapi_testing.async_fastapi_testing import (
    AsyncTestClient,
    AsyncTestResponse,
    AsyncTestServer,
    Config,
    InvalidResponseTypeError,
    PortGenerator,
    ServerStartupError,
    UvicornTestServer,
    WebSocketConfig,
    WebSocketHelper,
    create_test_server,
    get_config,
    set_config,
)

if TYPE_CHECKING:
    # For type checkers only; at runtime __getattr__ below serves the live value.
    from fastapi_testing.async_fastapi_testing import global_config

__all__ = [
    "AsyncTestClient",
    "AsyncTestResponse",
    "AsyncTestServer",
    "Config",
    "InvalidResponseTypeError",
    "PortGenerator",
    "ServerStartupError",
    "UvicornTestServer",
    "WebSocketConfig",
    "WebSocketHelper",
    "create_test_server",
    "get_config",
    "global_config",
    "set_config",
]


def __getattr__(name: str) -> object:
    # ``global_config`` is resolved on access so that it reflects set_config();
    # a plain re-export would keep returning the config active at import time.
    if name == "global_config":
        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
