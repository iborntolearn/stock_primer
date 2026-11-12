"""Runtime settings from environment / .env (prefix STOCK_PRIMER_)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STOCK_PRIMER_", env_file=ROOT / ".env", extra="ignore")

    # SQLite until Postgres is available; switch with STOCK_PRIMER_DATABASE_URL=postgresql+psycopg://...
    database_url: str = f"sqlite:///{(ROOT / 'data' / 'stock_primer.db').as_posix()}"
    data_dir: Path = ROOT / "data"
    cache_dir: Path = ROOT / "data" / "cache"

    universe: str = "NIFTY200"
    min_avg_traded_value_cr: float = 10.0
    min_price: float = 50.0
    benchmark: str = "NIFTY 500"

    # Reconciliation (§4.3)
    reconcile_sample_size: int = 20
    reconcile_close_tolerance: float = 0.01
    reconcile_volume_tolerance: float = 0.20

    # API (§4.7, §11): single user. If api_token is set, every request needs "Authorization: Bearer <token>".
    api_token: str = ""
    cors_origins: list[str] = ["http://localhost:4200"]

    # Claude: STOCK_PRIMER_ANTHROPIC_API_KEY in .env, or the standard ANTHROPIC_API_KEY environment variable
    anthropic_api_key: str = ""

    # Login (§11): password + authenticator code, checked by Caddy's forward_auth. Set by `python -m api.auth setup`.
    # When auth_password_hash is empty the API is open (development behind 127.0.0.1 only).
    auth_password_hash: str = ""
    auth_totp_secret: str = ""
    auth_session_key: str = ""
    auth_session_hours: int = 12
    auth_cookie_secure: bool = True

    # Backups (§11): nightly, gzip + AES-256-GCM, integrity-checked, rotated 7 daily / 4 weekly / 12 monthly.
    # backup_copy_dir (e.g. a OneDrive folder or USB drive) only ever receives encrypted files.
    backup_dir: Path = ROOT / "data" / "backups"
    backup_copy_dir: str = ""
    backup_key: str = ""
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 4
    backup_keep_monthly: int = 12

    # Telegram (§10). Secrets come from .env, never from code or logs.
    telegram_bot_token: str = ""
    telegram_owner_chat_id: int = 0

    # Data-quality gate (§4.3)
    freshness_min_fraction: float = 0.98
    unexplained_move_pct: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
