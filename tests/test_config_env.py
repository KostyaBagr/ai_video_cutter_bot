"""APP_ENV debug/prod helpers."""

import socket

from bot.config import Settings
from bot.main import _telegram_session


def test_is_debug_from_app_env() -> None:
    assert Settings(app_env="debug").is_debug is True
    assert Settings(app_env="DEV").is_debug is True
    assert Settings(app_env="prod").is_debug is False
    assert Settings(app_env="production").is_debug is False


def test_telegram_session_is_direct_ipv4() -> None:
    session = _telegram_session()
    assert session.proxy is None
    assert session._connector_init.get("family") == socket.AF_INET
