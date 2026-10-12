import json
import time

import pytest

from avito_personal_mcp.bot_panel import BotPanel
from avito_personal_mcp.monitor import WatchStore, validate_config
from avito_personal_mcp.notifications import NotificationService


class Secret:
    def load(self):
        return "not-a-real-token"


class Transport:
    calls = []
    updates = []

    def __init__(self, token):
        pass

    def call(self, method, data):
        self.calls.append((method, data))
        return self.updates if method == "getUpdates" else {"message_id": 1}


class Source:
    calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def search(self, config):
        Source.calls += 1
        return {
            "city_slug": "saratov",
            "results": [
                {
                    "id": i,
                    "title": f"Example product {i}",
                    "price": "1000 ₽",
                    "location": "Саратов",
                    "date": "Сегодня",
                    "url": f"https://www.avito.ru/saratov/tovary/example_{i}",
                }
                for i in range(1, 9)
            ],
        }


@pytest.fixture
def panel(tmp_path):
    Transport.calls, Transport.updates, Source.calls = [], [], 0
    store = WatchStore(tmp_path / "bot.db")
    store.save(validate_config("Saved query", city="Саратов"))
    notifications = NotificationService(store, Transport, Secret())
    with store.connect() as con:
        con.execute(
            "UPDATE notification_config SET chat_id=123,bot_username='ExampleBot',enabled=1"
        )
    return BotPanel(notifications, Source)


def update(text, identifier=1, sender=123, date=None):
    return {
        "update_id": identifier,
        "message": {
            "date": int(time.time()) if date is None else date,
            "chat": {"type": "private", "id": sender},
            "from": {"id": sender},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_only_owner_can_search_and_receives_exactly_five_results(panel):
    Transport.updates = [update("/search Example product", sender=456)]
    await panel.poll_once()
    assert Source.calls == 0
    assert not any(method == "sendMessage" for method, _ in Transport.calls)
    Transport.updates = [update("/search Example product", 2)]
    await panel.poll_once()
    messages = [data for method, data in Transport.calls if method == "sendMessage"]
    assert Source.calls == 1
    assert messages[-1]["text"].count("https://www.avito.ru/") == 5
    assert messages[-1]["chat_id"] == 123
    assert "Саратов" in messages[-1]["text"]


@pytest.mark.asyncio
async def test_callbacks_prompt_for_query_and_latest_uses_cache(panel):
    Transport.updates = [
        {
            "update_id": 1,
            "callback_query": {
                "id": "example-callback",
                "from": {"id": 123},
                "data": "panel.quick",
                "message": {"chat": {"type": "private", "id": 123}},
            },
        }
    ]
    await panel.poll_once()
    assert any(method == "answerCallbackQuery" for method, _ in Transport.calls)
    Transport.updates = [update("Example product", 2)]
    await panel.poll_once()
    assert Source.calls == 1
    Transport.updates = [update("/latest", 3)]
    await panel.poll_once()
    assert Source.calls == 1
    assert "Сохранённый поиск" in Transport.calls[-1][1]["text"]


@pytest.mark.asyncio
async def test_offsets_and_processed_updates_survive_restart(panel):
    Transport.updates = [update("/menu", 50)]
    await panel.poll_once()
    sends = sum(method == "sendMessage" for method, _ in Transport.calls)
    restarted = BotPanel(panel.notifications, Source)
    await restarted.poll_once()
    assert sum(method == "sendMessage" for method, _ in Transport.calls) == sends
    assert [data for method, data in Transport.calls if method == "getUpdates"][-1]["offset"] == 51


@pytest.mark.asyncio
async def test_avito_cooldown_is_not_bypassed_and_old_commands_are_not_replayed(panel):
    Transport.updates = [update("/search Product", date=int(time.time()) - 3600)]
    await panel.poll_once()
    assert Source.calls == 0
    with panel.store.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES('cooldown',?)", (time.time() + 1800,))
    Transport.updates = [update("/search Product", 2)]
    await panel.poll_once()
    assert Source.calls == 0
    assert "Авито ограничил" in Transport.calls[-1][1]["text"]


@pytest.mark.asyncio
async def test_latest_reads_newest_monitoring_events_even_after_first_hundred(panel):
    with panel.store.connect() as con:
        for i in range(110):
            con.execute(
                "INSERT INTO events(watch_id,listing_id,found_at,payload) VALUES(?,?,?,?)",
                (
                    "example",
                    str(i),
                    time.time(),
                    json.dumps(
                        {"title": f"Example {i}", "url": f"https://www.avito.ru/saratov/x_{i}"}
                    ),
                ),
            )
    Transport.updates = [update("/latest")]
    await panel.poll_once()
    text = Transport.calls[-1][1]["text"]
    assert "Example 109" in text and "Example 105" in text and "Example 104" not in text


@pytest.mark.asyncio
async def test_menu_is_scoped_to_paired_private_chat(panel):
    await panel.open()
    commands = next(data for method, data in Transport.calls if method == "setMyCommands")
    assert commands["scope"] == {"type": "chat", "chat_id": 123}
    assert Transport.calls[-1][1]["reply_markup"]["inline_keyboard"]
