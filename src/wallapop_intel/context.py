"""Contenedor de dependencias (singleton por proceso)."""

from __future__ import annotations

from wallapop_intel.client import WallapopClient
from wallapop_intel.config import Settings
from wallapop_intel.db import Store

_settings: Settings | None = None
_store: Store | None = None
_client: WallapopClient | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def client() -> WallapopClient:
    global _client
    if _client is None:
        cfg = settings()
        _client = WallapopClient(
            req_interval_seconds=cfg.req_interval_seconds,
            max_retries=cfg.max_retries,
            user_agent=cfg.user_agent,
            proxy_url=cfg.proxy_url,
        )
    return _client


def store() -> Store:
    global _store
    if _store is None:
        cfg = settings()
        _store = Store(cfg.database_url, cfg.snapshot_ttl_days)
    return _store


def reset_for_tests(cfg: Settings, db: Store, api: WallapopClient) -> None:
    global _settings, _store, _client
    _settings = cfg
    _store = db
    _client = api
