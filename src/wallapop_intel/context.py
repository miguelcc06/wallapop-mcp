"""Estado compartido del proceso: cliente HTTP y SQLite."""

from __future__ import annotations

from wallapop_intel.client import WallapopClient
from wallapop_intel.config import Settings
from wallapop_intel.db import Store

_settings: Settings | None = None
_client: WallapopClient | None = None
_store: Store | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def client() -> WallapopClient:
    global _client
    if _client is None:
        _client = WallapopClient(settings())
    return _client


def store() -> Store:
    global _store
    if _store is None:
        cfg = settings()
        _store = Store(cfg.db_path, cfg.snapshot_ttl_days)
    return _store


def reset_for_tests(cfg: Settings, db: Store, api: WallapopClient) -> None:
    global _settings, _client, _store
    _settings = cfg
    _client = api
    _store = db
