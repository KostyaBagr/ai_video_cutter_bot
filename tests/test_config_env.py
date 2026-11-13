"""APP_ENV debug/prod helpers."""

from bot.config import Settings


def test_is_debug_from_app_env() -> None:
    assert Settings(app_env="debug").is_debug is True
    assert Settings(app_env="DEV").is_debug is True
    assert Settings(app_env="prod").is_debug is False
    assert Settings(app_env="production").is_debug is False
