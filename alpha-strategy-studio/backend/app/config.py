import os
from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_name = os.getenv("APP_NAME", "Alpha Strategy Studio")
    iifl_base = os.getenv("IIFL_API_BASE_URL", "https://api.iiflcapital.com/v1").rstrip("/")
    app_key = os.getenv("IIFL_APP_KEY", "")
    app_secret = os.getenv("IIFL_APP_SECRET", "")
    redirect_url = os.getenv("IIFL_REDIRECT_URL", "http://127.0.0.1:8000/api/iifl/callback")
    store_backend = os.getenv("STORE_BACKEND", "json").strip().lower() or "json"
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
    mongodb_db = os.getenv("MONGODB_DB", "alpha_strategy_studio")
    levels_placeholder_enabled = _flag("LEVELS_PLACEHOLDER_ENABLED", "false")
    sar_worker_enabled = _flag("SAR_WORKER_ENABLED", "true")
    paper_worker_enabled = _flag("PAPER_WORKER_ENABLED", "true")
    live_execution_enabled = _flag("LIVE_EXECUTION_ENABLED", "false")


settings = Settings()
