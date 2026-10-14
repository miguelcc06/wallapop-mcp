"""Monitor SFF: filtro determinista, deduplicación SQLite y mensaje de Telegram."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from wallapop_intel.monitor import (
    DEFAULT_CHAT_ID,
    Listing,
    SeenStore,
    TelegramConfigError,
    format_telegram_message,
    is_sff_candidate,
    load_telegram_credentials,
    main,
    scan_once,
    send_telegram,
)


def _payload(items: list[dict]) -> dict:
    return {"data": {"section": {"payload": {"items": items}}}}


def _pc(
    item_id: str = "sff1",
    *,
    title: str = "HP ProDesk 600 G3 SFF i5",
    price: float = 40,
    city: str = "Madrid",
    shippable: bool = True,
    description: str | None = "Torre SFF completa, enciende",
) -> dict:
    raw: dict = {
        "id": item_id,
        "title": title,
        "price": {"amount": price, "currency": "EUR"},
        "location": {"city": city},
        "web_slug": f"{item_id}-slug",
        "shipping": {"item_is_shippable": shippable, "user_allows_shipping": shippable},
    }
    if description is not None:
        raw["description"] = description
    return raw


class FakeClient:
    def __init__(self, pages: dict[str, dict], details: dict[str, dict] | None = None) -> None:
        self.pages = pages
        self.details = details or {}
        self.calls: list[tuple[str, str, float]] = []
        self.detail_calls: list[str] = []

    async def search(self, keywords: str, *, order_by: str = "newest", max_price: float | None = None, **_kwargs):
        self.calls.append((keywords, order_by, float(max_price)))
        return self.pages.get(keywords, _payload([]))

    async def get_item(self, item_id: str) -> dict:
        self.detail_calls.append(item_id)
        return self.details.get(item_id, {"id": item_id})


@pytest.mark.parametrize(
    ("title", "description", "price", "expected"),
    [
        ("HP ProDesk 600 G3 SFF i5", "Equipo completo", 40, True),
        ("HP EliteDesk 800 G3 Mini", None, 50, True),
        ("OptiPlex 3060 SFF", "Funciona", 1, True),
        ("ThinkCentre M710s SFF", "Buen estado", 49.5, True),
        ("HP ProDesk fantastico", None, 25, True),
        ("Cable SATA HP ProDesk", None, 10, False),
        ("Memoria RAM 8GB DDR4 HP", None, 15, False),
        ("Módulo RAM 16GB", None, 20, False),
        ("Fuente de alimentación 180W", None, 12, False),
        ("Power supply HP 600 G2", None, 18, False),
        ("Teclado y ratón HP", None, 8, False),
        ("Mouse inalámbrico", None, 5, False),
        ("Ventilador fan 80mm", None, 6, False),
        ("Solo CPU i5-6500", None, 22, False),
        ("Lote de procesadores", None, 30, False),
        ("Despiece HP ProDesk", "piezas sueltas", 35, False),
        ("Tapa HP ProDesk", None, 4, False),
        ("HP ProDesk 600 G3 SFF", "Equipo completo", 0, False),
        ("HP ProDesk 600 G3 SFF", "Equipo completo", 50.01, False),
        ("HP ProDesk 600 G3 SFF", "Equipo completo", None, False),
        ("HP ProDesk 600 G3 SFF i5", "8G memoria ram y cable de corriente", 45, True),
        (
            "Ordenador pc HP prodesk 400 G4 i3",
            "No tiene memoria ram. Con ram funciona perfectamente.",
            50,
            True,
        ),
        ("HP EliteDesk G1 DM Mini PC", "120 SSD y 8gb ram, i5-4 gen", 50, True),
        (
            "ThinkCentre M710s SFF i3",
            "Se vende para piezas. Incluye teclado y ratón. No arranca.",
            44.99,
            False,
        ),
        (
            "Despiece HP ProDesk barebone",
            "Chasis con placa base y CPU i5 funcionando",
            30,
            True,
        ),
        (
            "HP ProDesk 600 G3 SFF",
            "Vendo la fuente, el PC no está",
            40,
            False,
        ),
        (
            "Despiece barebone chasis con placa base cpu i5 funcionando y teclado",
            None,
            30,
            False,
        ),
    ],
)
def test_sff_filter(title: str, description: str | None, price: float | None, expected: bool) -> None:
    assert is_sff_candidate(title, description, price) is expected


def test_schema_and_second_scan_does_not_realert(tmp_path: Path) -> None:
    raw = _pc()
    client = FakeClient({"hp prodesk": _payload([raw])})
    store = SeenStore(tmp_path / "sff_monitor.db")
    columns = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(seen_items)").fetchall()
    }
    assert columns == {
        "item_id",
        "title",
        "price",
        "shippable",
        "city",
        "url",
        "first_seen_at",
        "notified_at",
    }
    sent: list[str] = []

    async def run() -> None:
        first = await scan_once(
            client,
            store=store,
            notify=sent.append,
            queries=("hp prodesk", "hp elitedesk"),
        )
        second = await scan_once(
            client,
            store=store,
            notify=sent.append,
            queries=("hp prodesk",),
        )
        assert len(first) == 1
        assert second == []

    asyncio.run(run())
    assert len(sent) == 1
    assert "HP ProDesk 600 G3 SFF i5" in sent[0]
    assert store.seen("sff1")
    assert client.calls
    assert {call[1] for call in client.calls} == {"newest"}
    assert {call[2] for call in client.calls} == {50.0}
    store.close()


def test_same_item_in_two_queries_alerts_once(tmp_path: Path) -> None:
    raw = _pc(item_id="dup")
    client = FakeClient({"hp prodesk": _payload([raw]), "prodesk sff": _payload([raw])})
    store = SeenStore(tmp_path / "sff.db")
    sent: list[str] = []
    asyncio.run(
        scan_once(
            client,
            store=store,
            notify=sent.append,
            queries=("hp prodesk", "prodesk sff"),
        )
    )
    assert len(sent) == 1
    store.close()


def test_description_from_detail_filters_parts(tmp_path: Path) -> None:
    raw = _pc(item_id="parts", title="HP ProDesk 600 G3 SFF", description=None, price=35)
    client = FakeClient(
        {"hp prodesk": _payload([raw])},
        details={"parts": {"id": "parts", "description": "Solo la fuente de alimentación"}},
    )
    store = SeenStore(tmp_path / "sff.db")
    sent: list[str] = []
    found = asyncio.run(
        scan_once(client, store=store, notify=sent.append, queries=("hp prodesk",))
    )
    assert found == []
    assert sent == []
    assert client.detail_calls == ["parts"]
    assert not store.seen("parts")
    store.close()


def test_dry_run_prints_without_db_or_telegram(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    raw = _pc(price=42, city="Valencia", shippable=False)
    client = FakeClient({"hp prodesk": _payload([raw])})
    store = SeenStore(tmp_path / "sff.db")
    sent: list[str] = []
    found = asyncio.run(
        scan_once(
            client,
            store=store,
            notify=sent.append,
            dry_run=True,
            queries=("hp prodesk",),
        )
    )
    assert len(found) == 1
    assert sent == []
    assert not store.seen("sff1")
    printed = capsys.readouterr().out
    assert "HP ProDesk 600 G3 SFF i5" in printed
    assert "📍 Solo en mano" in printed
    store.close()


def test_telegram_message_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = Listing(
        item_id="abc",
        title="HP ProDesk 600_G3 *SFF*",
        price=45,
        shippable=True,
        city="Madrid",
        url="https://es.wallapop.com/item/hp-prodesk",
    )
    text = format_telegram_message(listing)
    assert text.splitlines() == [
        "🖥️ *Nuevo PC SFF en Wallapop (< 50€)*",
        "• *Título:* HP ProDesk 600\\_G3 \\*SFF\\*",
        "• *Precio:* 45 €",
        "• *Envío:* 📦 Con envío",
        "• *Ubicación:* Madrid",
        "• *Enlace:* https://es.wallapop.com/item/hp-prodesk",
    ]
    pickup = format_telegram_message(
        Listing("x", "OptiPlex SFF", 19.5, False, None, None)
    )
    assert "• *Precio:* 19.5 €" in pickup
    assert "📍 Solo en mano" in pickup
    assert "Sin ubicación" in pickup
    assert "sin enlace" in pickup

    captured: dict = {}

    class Resp:
        status_code = 200
        text = '{"ok": true}'

        def json(self) -> dict:
            return {"ok": True}

        def raise_for_status(self) -> None:
            return None

    def post(url: str, json: dict, timeout: float) -> Resp:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(httpx, "post", post)
    send_telegram("TOKEN", DEFAULT_CHAT_ID, text)
    assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert captured["json"]["chat_id"] == DEFAULT_CHAT_ID
    assert captured["json"]["text"] == text
    assert captured["json"]["parse_mode"] == "Markdown"
    assert captured["timeout"] == 20.0


def test_telegram_credentials_env_and_openclaw(tmp_path: Path) -> None:
    token, chat = load_telegram_credentials(
        environ={"TELEGRAM_BOT_TOKEN": "from-env", "TELEGRAM_CHAT_ID": "111"},
        openclaw_path=tmp_path / "missing.json",
    )
    assert (token, chat) == ("from-env", "111")

    path = tmp_path / "openclaw.json"
    path.write_text(
        json.dumps(
            {
                "channels": {
                    "telegram": {
                        "accounts": {
                            "oliver_tree": {"botToken": "tree-token", "chatId": "222"},
                            "wallapop": {"botToken": "walla-token"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    token, chat = load_telegram_credentials(environ={}, openclaw_path=path)
    assert token == "walla-token"
    assert chat == DEFAULT_CHAT_ID

    only_tree = tmp_path / "tree.json"
    only_tree.write_text(
        json.dumps({"accounts": {"oliver_tree": {"botToken": "tree-only"}}}),
        encoding="utf-8",
    )
    token, chat = load_telegram_credentials(environ={}, openclaw_path=only_tree)
    assert token == "tree-only"
    assert chat == DEFAULT_CHAT_ID

    with pytest.raises(TelegramConfigError):
        load_telegram_credentials(environ={}, openclaw_path=tmp_path / "no.json")


def test_cli_dry_run_skips_db_and_second_pass_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _pc()

    class Dummy:
        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def search(self, keywords: str, **_kwargs):
            if keywords == "hp prodesk":
                return _payload([raw])
            return _payload([])

        async def get_item(self, item_id: str) -> dict:
            return {"id": item_id}

    monkeypatch.setattr("wallapop_intel.monitor.WallapopClient", lambda _settings: Dummy())
    sent: list[str] = []
    monkeypatch.setattr(
        "wallapop_intel.monitor.send_telegram",
        lambda token, chat_id, text: sent.append(text),
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    db = tmp_path / "nested" / "sff_monitor.db"

    assert main(["--dry-run", "--once", "--db-path", str(db)]) == 0
    assert not db.exists()
    assert sent == []
    assert "HP ProDesk 600 G3 SFF i5" in capsys.readouterr().out

    assert main(["--once", "--db-path", str(db)]) == 0
    assert main(["--once", "--db-path", str(db)]) == 0
    assert len(sent) == 1
    assert db.is_file()
