"""FastAPI application factory.

Day 1 exposes only a health endpoint; agent and ticket endpoints arrive
with later days.
"""

from fastapi import FastAPI

from app import __version__
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="SupportFlow", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "app_env": settings.app_env,
            "llm_configured": str(settings.has_deepseek_key).lower(),
        }

    return app
