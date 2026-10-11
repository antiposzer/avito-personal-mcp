from datetime import datetime, timedelta
from pathlib import Path

import pytest

from avito_personal_mcp.monitor import (
    SARATOV,
    MonitorManager,
    WatchStore,
    assess,
    publication_time,
    validate_config,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=SARATOV)


def listing(number=1, **changes):
    return dict(
        id=number,
        title="Example product",
        price="1000 ₽",
        condition="Новое",
        location="Саратов",
        rating="4,8",
        date="5 минут назад",
        url=f"https://www.avito.ru/saratov/tovary/example_{number}",
        **changes,
    )


class Source:
    def __init__(self, items, fail=None, details=None):
        self.items, self.fail, self.details = items, fail, details
        self.opened = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def search(self, config):
        if self.fail:
            raise RuntimeError(self.fail)
        return {"results": self.items, "city_slug": "saratov"}

    async def detail(self, item):
        self.opened += 1
        return self.details or dict(
            description="Продаю личный телефон, пользовался сам.",
            params=["Состояние: Б/у"],
            date="сегодня в 11:55",
            date_iso="2026-10-11T11:55:00+04:00",
            date_precision="minute",
        )


@pytest.mark.asyncio
async def test_baseline_restart_and_event_cursor_do_not_repeat_old_ads(tmp_path: Path):
    store = WatchStore(tmp_path / "monitor.db")
    wid = store.save(validate_config("Any product"))["id"]
    source = Source([listing()])
    manager = MonitorManager(store, lambda: source)
    await manager.poll_once(NOW.timestamp())
    assert store.events() == []
    source.items = [listing(2), listing()]
    restarted = MonitorManager(WatchStore(store.path), lambda: source)
    await restarted.poll_once(NOW.timestamp() + 301)
    events = store.events()
    assert [e["listing_id"] for e in events] == ["2"]
    source.items[0]["price"] = "900 ₽"
    await restarted.poll_once(NOW.timestamp() + 602)
    assert len(store.events()) == 1
    assert store.events(events[-1]["id"]) == []
    assert store.events(watch_id=wid)[0]["payload"]["first_seen_at"]


@pytest.mark.asyncio
async def test_http_429_cooldown_applies_to_all_searches_and_does_not_mark_seen(tmp_path):
    store = WatchStore(tmp_path / "state.db")
    store.save(validate_config("First"))
    source = Source([], fail="HTTP 429")
    manager = MonitorManager(store, lambda: source)
    await manager.poll_once(NOW.timestamp())
    store.save(validate_config("Second"))
    assert store.claim(NOW.timestamp() + 301) is None
    assert store.list()[0]["error"] == "rate_limited"
    assert store.list()[0]["initialized"] == 0
    assert store.claim(NOW.timestamp() + 1801) is not None


def test_only_one_process_claims_a_due_search(tmp_path):
    store = WatchStore(tmp_path / "state.db")
    store.save(validate_config("Search"))
    assert store.claim(NOW.timestamp()) is not None
    assert WatchStore(store.path).claim(NOW.timestamp()) is None


def test_all_five_filters_and_unknowns_fail_closed():
    config = validate_config("Any query", "Саратов", "new", "private", 4.5, 10)
    item = listing()
    item["description"] = "Продаю свой телефон. Пользовался сам."
    assert assess(item, config, NOW, "saratov")[0]
    for key, value in [
        ("rating", None),
        ("rating", "4.0"),
        ("date", "недавно"),
        ("description", None),
        ("condition", "Не новый"),
    ]:
        candidate = dict(item, **{key: value})
        assert not assess(candidate, config, NOW, "saratov")[0]
    other = dict(item, location="Москва", url="https://www.avito.ru/moskva/tovary/x_2")
    assert not assess(other, config, NOW, "saratov")[0]
    district = dict(item, location="Ленинский район")
    assert assess(district, config, NOW, "saratov")[0]
    assert not assess(district, config, NOW, None)[0]
    assert not assess(dict(item, condition="Как новый"), config, NOW, "saratov")[0]


def test_publication_time_is_not_first_seen_time():
    date = publication_time("15 минут назад", NOW)
    assert datetime.fromisoformat(date["date_iso"]) == NOW - timedelta(minutes=15)
    config = validate_config("Search", max_age_minutes=10)
    item = dict(listing(), publication=publication_time("5 минут назад", NOW))
    assert not assess(item, config, NOW + timedelta(minutes=20), None)[0]
    assert not assess(dict(listing(), date="сегодня"), config, NOW, None)[0]
    assert not assess(dict(listing(), date="11 октября 2026 в 13:00"), config, NOW, None)[0]


@pytest.mark.parametrize(
    "changes",
    [
        dict(query=" "),
        dict(condition="excellent"),
        dict(seller_type="verified"),
        dict(min_rating=float("nan")),
        dict(min_rating=5.1),
        dict(max_age_minutes=True),
        dict(max_age_minutes=0),
    ],
)
def test_invalid_criteria(changes):
    with pytest.raises(ValueError):
        validate_config(**dict(dict(query="Search"), **changes))


@pytest.mark.asyncio
async def test_detail_budget_defers_candidates_without_losing_them(tmp_path):
    store = WatchStore(tmp_path / "state.db")
    store.save(validate_config("Search", seller_type="private"))
    source = Source([])
    manager = MonitorManager(store, lambda: source)
    await manager.poll_once(NOW.timestamp())
    source.items = [listing(i) for i in range(1, 13)]
    await manager.poll_once(NOW.timestamp() + 301)
    assert source.opened == 10
    assert len(store.events()) == 10
    assert store.list()[0]["counts"]["pending"] == 2
    await manager.poll_once(NOW.timestamp() + 602)
    assert len(store.events()) == 12


def test_pause_and_changed_criteria(tmp_path):
    store = WatchStore(tmp_path / "state.db")
    config = validate_config("Query")
    wid = store.save(config)["id"]
    assert store.save(config)["id"] == wid
    store.enable(wid, False)
    assert store.claim(NOW.timestamp()) is None
    changed = store.save(validate_config("Changed"), wid)
    assert changed["initialized"] == 0
    assert not changed["enabled"]


@pytest.mark.asyncio
async def test_filtered_cards_do_not_waste_detail_requests_and_unknown_condition_is_loaded(
    tmp_path,
):
    store = WatchStore(tmp_path / "filters.db")
    store.save(validate_config("Product", condition="used", seller_type="private", min_rating=4.5))
    source = Source([])
    manager = MonitorManager(store, lambda: source)
    await manager.poll_once(NOW.timestamp())
    low = dict(listing(1), rating="3.0")
    candidate = dict(listing(2), condition="256 ГБ, SIM + eSIM")
    source.items = [low, candidate]
    await manager.poll_once(NOW.timestamp() + 301)
    assert source.opened == 1
    assert [e["listing_id"] for e in store.events()] == ["2"]
    assert store.events()[0]["payload"]["condition"] == "Б/у"
