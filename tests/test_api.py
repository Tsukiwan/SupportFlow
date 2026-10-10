"""FastAPI app factory and health endpoint."""

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import get_settings


def test_health_endpoint_without_key(monkeypatch, tmp_path):
    # chdir away from the repo so a real .env file cannot leak into the test
    monkeypatch.chdir(tmp_path)
    for var in (
        "SUPPORTFLOW_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
        "SUPPORTFLOW_APP_ENV",
    ):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.get("/health")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_env"] == "development"
    assert body["llm_configured"] == "false"
