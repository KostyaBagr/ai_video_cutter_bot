"""APP_ENV debug/prod helpers and Telegram proxy session."""

import socket

from bot.config import Settings, settings
from bot.main import _proxy_label, _telegram_session


def test_is_debug_from_app_env() -> None:
    assert Settings(app_env="debug").is_debug is True
    assert Settings(app_env="DEV").is_debug is True
    assert Settings(app_env="prod").is_debug is False
    assert Settings(app_env="production").is_debug is False


def test_proxy_label_hides_credentials() -> None:
    assert (
        _proxy_label("socks5://user:secret@1.2.3.4:1080")
        == "socks5://1.2.3.4:1080"
    )


def test_telegram_session_uses_socks_proxy(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_proxy", "socks5://127.0.0.1:1080")
    session = _telegram_session()
    assert session.proxy == "socks5://127.0.0.1:1080"


def test_telegram_session_direct_is_ipv4(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_proxy", "  ")
    session = _telegram_session()
    assert session.proxy is None
    assert session._connector_init.get("family") == socket.AF_INET
