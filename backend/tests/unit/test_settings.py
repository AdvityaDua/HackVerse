from app.infrastructure.config import get_settings


def test_settings_load_defaults() -> None:
    settings = get_settings()

    assert settings.app_name == "HackVerse API"
    assert settings.api_prefix == "/api/v1"
