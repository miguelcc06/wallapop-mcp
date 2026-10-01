"""Resolución de WALLAPOP_DATABASE_URL sin abrir PostgreSQL."""

from pathlib import Path

from wallapop_intel.config import DEFAULT_DATABASE_URL, resolve_database_url


def test_wallapop_database_url_wins(monkeypatch) -> None:
    monkeypatch.setenv("WALLAPOP_DATABASE_URL", "postgresql://example/wallapop_intel")
    monkeypatch.setenv("DATABASE_URL", "postgresql://other/db")
    assert resolve_database_url() == "postgresql://example/wallapop_intel"


def test_database_url_is_second(monkeypatch) -> None:
    monkeypatch.delenv("WALLAPOP_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://other/db")
    assert resolve_database_url() == "postgresql://other/db"


def test_blank_urls_fall_through(monkeypatch) -> None:
    monkeypatch.setenv("WALLAPOP_DATABASE_URL", "  ")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr("wallapop_intel.config._unix_socket_dir", lambda: None)
    assert resolve_database_url() == DEFAULT_DATABASE_URL


def test_unix_socket_when_present(monkeypatch) -> None:
    monkeypatch.delenv("WALLAPOP_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("wallapop_intel.config._unix_socket_dir", lambda: Path("/var/run/postgresql"))
    assert resolve_database_url() == "postgresql://miguelcc06@/wallapop_intel?host=/var/run/postgresql"


def test_settings_exposes_database_url(monkeypatch) -> None:
    monkeypatch.setenv("WALLAPOP_DATABASE_URL", "postgresql://example/wallapop_intel")
    from wallapop_intel.config import Settings

    assert Settings.from_env().database_url == "postgresql://example/wallapop_intel"
