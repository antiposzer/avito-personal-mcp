"""Verify delayed description hydration and rendered paragraph boundaries."""

from datetime import datetime

import pytest
from playwright.async_api import Error, async_playwright

from avito_personal_mcp import listing_detail
from avito_personal_mcp.listing_date import SARATOV
from avito_personal_mcp.listing_detail import discover_listing_detail


@pytest.mark.asyncio
async def test_public_description_waits_for_hydration_and_preserves_line_breaks(monkeypatch):
    monkeypatch.setattr(
        listing_detail, "now_in_saratov", lambda: datetime(2026, 10, 6, 22, 30, tzinfo=SARATOV)
    )
    url = "https://www.avito.ru/saratov/telefony/phone_1234567890"
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Error as exc:
            if "not found" in str(exc) or "doesn't exist" in str(exc):
                pytest.skip("DOM acceptance requires an installed Chrome browser")
            raise
        try:
            context = await browser.new_context(offline=True)
            page = await context.new_page()
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body="""<h1 data-marker="item-view/title-info">Phone</h1>
                  <p><span data-marker="item-view/item-id">№&nbsp;1234567890</span>
                  <span data-marker="item-view/item-date"> · <!-- -->вчера в 09:15</span>
                  <span data-marker="item-view/total-views">143&nbsp;просмотра</span>
                  <span data-marker="item-view/today-views">&nbsp;<!-- -->(+56&nbsp;сегодня)</span>
                  </p>
                  <div data-marker="item-view/item-description"></div>
                  <script>setTimeout(() => {
                    document.querySelector('[data-marker="item-view/item-description"]')
                      .innerHTML = '<p>First line<br>Second line</p><p>Last paragraph</p>';
                  }, 1300);</script>""",
                ),
            )
            result = await discover_listing_detail(page, "https://www.avito.ru", url)
            assert result["description"] == "First line\nSecond line\n\nLast paragraph"
            assert result["id"] == 1234567890
            assert result["date"] == "вчера в 09:15"
            assert result["date_iso"] == "2026-10-05T09:15:00+04:00"
            assert result["date_source"] == "item-view/item-date"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_closed_listing_expands_description_and_keeps_inactive_state():
    url = "https://www.avito.ru/saratov/telefony/phone_1234567890"
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Error as exc:
            if "not found" in str(exc) or "doesn't exist" in str(exc):
                pytest.skip("DOM acceptance requires an installed Chrome browser")
            raise
        try:
            context = await browser.new_context(offline=True)
            page = await context.new_page()
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body="""<h1 data-marker="item-view/title-info">Phone</h1>
                  <div data-marker="item-view/closed-warning">Объявление снято с публикации.</div>
                  <div data-marker="item-view/close-extended-button">Показать описание</div>
                  <script>
                    window.expansions = 0;
                    const control = document.querySelector(
                      '[data-marker="item-view/close-extended-button"]'
                    );
                    control.addEventListener('click', () => {
                      window.expansions++;
                      control.style.display = 'none';
                      const description = document.createElement('div');
                      description.setAttribute('data-marker', 'item-view/item-description');
                      description.innerHTML = '<p>Archived full description</p>';
                      document.body.appendChild(description);
                    });
                  </script>""",
                ),
            )
            result = await discover_listing_detail(page, "https://www.avito.ru", url)
            assert result["description"] == "Archived full description"
            assert result["state"] == "inactive"
            assert await page.evaluate("window.expansions") == 1
        finally:
            await browser.close()
