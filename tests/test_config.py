"""Settings: env-driven config, safe without an API key, no key leakage."""

from app.config import Settings, get_settings


def test_defaults_without_env_or_file(monkeypatch):
    monkeypatch.delenv("SUPPORTFLOW_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SUPPORTFLOW_DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.deepseek_api_key is None
    assert settings.has_deepseek_key is False
    assert settings.deepseek_model == "deepseek-chat"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.max_agent_iterations == 10
    assert settings.tool_timeout_seconds == 30.0


def test_prefixed_env_names_are_applied(monkeypatch):
    monkeypatch.setenv("SUPPORTFLOW_DEEPSEEK_MODEL", "deepseek-reasoner")
    monkeypatch.setenv("SUPPORTFLOW_MAX_AGENT_ITERATIONS", "25")
    settings = Settings(_env_file=None)
    assert settings.deepseek_model == "deepseek-reasoner"
    assert settings.max_agent_iterations == 25


def test_bare_deepseek_env_names_are_applied(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-env-value")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    settings = Settings(_env_file=None)
    assert settings.has_deepseek_key is True
    assert settings.deepseek_model == "deepseek-chat"
    assert "sk-deepseek-env-value" not in repr(settings)


def test_settings_read_key_from_dotenv_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-deepseek-file-value\n"
        "DEEPSEEK_MODEL=deepseek-reasoner\n"
    )
    settings = Settings(_env_file=env_file)
    assert settings.has_deepseek_key is True
    assert settings.deepseek_model == "deepseek-reasoner"
    assert "sk-deepseek-file-value" not in repr(settings)


def test_missing_key_still_allows_construction(monkeypatch):
    monkeypatch.delenv("SUPPORTFLOW_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # Importing app.config must never fail, and constructing settings must
    # not require the key — local tests run without one.
    settings = Settings(_env_file=None)
    assert settings.deepseek_api_key is None
    assert settings.has_deepseek_key is False


def test_empty_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    settings = Settings(_env_file=None)
    assert settings.has_deepseek_key is False


def test_unknown_vars_are_ignored(monkeypatch):
    monkeypatch.setenv("SUPPORTFLOW_UNKNOWN_OPTION", "1")
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"


def test_get_settings_is_cached():
    get_settings.cache_clear()
    assert get_settings() is get_settings()
