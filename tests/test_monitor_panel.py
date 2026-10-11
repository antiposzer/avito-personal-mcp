import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from playwright.async_api import Error, async_playwright

from avito_personal_mcp.monitor import WatchStore
from avito_personal_mcp.monitor_panel import MonitorPanel
from avito_personal_mcp.notifications import NotificationService


def test_panel_rejects_cross_site_mutations_and_escapes_listing_text(tmp_path):
    panel = MonitorPanel(WatchStore(tmp_path / "state.db"))
    try:
        with urlopen(panel.url) as response:
            html = response.read().decode()
            assert "textContent" in html
            assert "innerHTML" not in html
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        body = json.dumps(dict(query="Any product", city="Саратов")).encode()
        request = Request(
            panel.url + "api/save",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.invalid",
                "X-Monitor-Token": panel.token,
            },
        )
        with pytest.raises(HTTPError) as failure:
            urlopen(request)
        assert failure.value.code == 403
        assert panel.store.list() == []
        request.headers["Origin"] = panel.url.rstrip("/")
        with urlopen(request) as response:
            saved = json.loads(response.read())
        assert saved["config"]["query"] == "Any product"
        assert len(panel.store.list()) == 1
        with urlopen(panel.url + "api/state?after_id=0") as response:
            state = json.loads(response.read())
        assert state["events"] == []
        bad = Request(panel.url, headers={"Host": "evil.invalid"})
        with pytest.raises(HTTPError) as failure:
            urlopen(bad)
        assert failure.value.code == 403
    finally:
        panel.close()


@pytest.mark.asyncio
async def test_telegram_panel_clears_token_pairs_private_chat_and_sends_only_on_test_click(
    tmp_path,
):
    from test_notifications import FakeTelegram, MemorySecret

    FakeTelegram.sends, FakeTelegram.updates = [], []
    FakeTelegram.failure, FakeTelegram.webhook = None, False
    store = WatchStore(tmp_path / "telegram-ui.db")
    notifications = NotificationService(store, FakeTelegram, MemorySecret())
    panel = MonitorPanel(store, notifications)
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Error as exc:
                if "not found" in str(exc) or "doesn't exist" in str(exc):
                    pytest.skip("Panel acceptance requires installed Chrome")
                raise
            try:
                page = await browser.new_page()
                await page.goto(panel.url)
                await page.locator("#tg-token").fill("fake-token-for-ui-test")
                await page.locator("#tg-setup").click()
                await page.locator("#tg-confirm").wait_for()
                assert await page.locator("#tg-token").input_value() == ""
                assert FakeTelegram.sends == []
                import time

                FakeTelegram.updates = [
                    {
                        "message": {
                            "text": "/start " + notifications.pairing["nonce"],
                            "date": int(time.time()),
                            "chat": {"id": 123, "type": "private"},
                            "from": {"id": 123},
                        }
                    }
                ]
                await page.locator("#tg-confirm").click()
                await page.locator("#tg-test").wait_for()
                assert FakeTelegram.sends == []
                await page.locator("#tg-test").click()
                await page.wait_for_function(
                    "() => document.querySelector('#tg-message').textContent.includes('отправлено')"
                )
                assert len(FakeTelegram.sends) == 1
                assert FakeTelegram.sends[0]["chat_id"] == 123
            finally:
                await browser.close()
    finally:
        panel.close()


@pytest.mark.asyncio
async def test_user_can_create_and_pause_a_universal_search_in_the_panel(tmp_path):
    panel = MonitorPanel(WatchStore(tmp_path / "web.db"))
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Error as exc:
                if "not found" in str(exc) or "doesn't exist" in str(exc):
                    pytest.skip("Panel acceptance requires installed Chrome")
                raise
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(panel.url)
                await page.locator("#query").fill("Example product")
                await page.locator("#city").fill("Саратов")
                await page.locator("#condition").select_option("used")
                await page.locator("#seller_type").select_option("private")
                await page.locator("#min_rating").fill("4.5")
                await page.locator("#max_age_minutes").fill("30")
                await page.get_by_role("button", name="Начать мониторинг").click()
                await page.get_by_role("button", name="Приостановить").wait_for()
                saved = panel.store.list()[0]
                assert saved["config"] == {
                    "query": "Example product",
                    "city": "Саратов",
                    "condition": "used",
                    "seller_type": "private",
                    "min_rating": 4.5,
                    "max_age_minutes": 30,
                }
                await page.get_by_role("button", name="Приостановить").click()
                await page.get_by_role("button", name="Возобновить").wait_for()
                assert not panel.store.list()[0]["enabled"]
                assert errors == []
            finally:
                await browser.close()
    finally:
        panel.close()
