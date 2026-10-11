"""Exercise the real DOM extractor against the observed search-card layout."""

import pytest
from playwright.async_api import Error, async_playwright

from avito_personal_mcp import search


@pytest.fixture
async def offline_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Error as exc:
            if "not found" in str(exc) or "doesn't exist" in str(exc):
                pytest.skip("DOM acceptance requires an installed Chrome browser")
            raise
        try:
            context = await browser.new_context(offline=True)
            yield await context.new_page()
        finally:
            await browser.close()


# Sanitized version of the user's observed structure: price is in a smaller
# ancestor than condition, district, rating and both kinds of badge.
CARD_HTML = """
<div data-marker="catalog-serp">
  <div data-marker="item">
    <div><a data-marker="item-title" href="/saratov/telefony/phone_1234567890">
      Phone A
    </a><span data-marker="item-price-value">52 990 ₽</span></div>
    <span data-marker="iva-item/2353">Цена ниже рыночной</span>
    <div data-marker="item-specific-params">Новый</div>
    <div data-marker="item-location">р-н Фрунзенский</div>
    <span data-marker="seller-rating/score">4,8</span>
    <span data-marker="badge-title-2359">Надёжный продавец</span>
  </div>
  <div data-marker="item">
    <div><a data-marker="item-title" href="/saratov/telefony/phone_1234567891">
      Phone B
    </a><span data-marker="item-price-value">40 000 ₽</span></div>
    <div data-marker="item-specific-params">Б/у</div>
    <div data-marker="item-location">р-н Кировский</div>
    <span data-marker="seller-rating/score">4,7</span>
    <span data-marker="iva-item/2558">Оплата частями</span>
  </div>
  <div data-marker="item">
    <a data-marker="item-title" href="/saratov/telefony/phone_1234567892">Phone C</a>
  </div>
</div>
"""


@pytest.mark.asyncio
async def test_card_fields_use_full_card_and_never_leak_from_neighbours(monkeypatch):
    async def no_search_navigation(*args):
        pass

    monkeypatch.setattr(search, "_submit_search_form", no_search_navigation)
    async with async_playwright() as playwright:
        # Use a local browser only; never attach to the user's authenticated
        # browser, open profiles, fetch external assets or contact Avito.
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Error as exc:
            if "not found" in str(exc) or "doesn't exist" in str(exc):
                pytest.skip("DOM acceptance requires an installed Chrome browser")
            raise
        try:
            context = await browser.new_context(offline=True)
            page = await context.new_page()
            await page.set_content(CARD_HTML)
            results = await search.search_avito(page, "https://www.avito.ru", "Phone", 3)
            assert [(r["condition"], r["location"], r["rating"], r["badges"]) for r in results] == [
                ("Новый", "р-н Фрунзенский", "4,8", ["Цена ниже рыночной", "Надёжный продавец"]),
                ("Б/у", "р-н Кировский", "4,7", ["Оплата частями"]),
                (None, None, None, []),
            ]
            limited = await search.search_avito(page, "https://www.avito.ru", "Phone", 1)
            assert limited == results[:1]
            assert page.url == "about:blank"
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "requested"), [(False, True), (True, False), (True, True), (False, False)]
)
async def test_local_priority_sets_state_once_and_waits_for_refresh(
    offline_page, initial: bool, requested: bool
):
    page = offline_page
    await page.set_content(f"""
      <div data-marker="catalog-serp"><a data-marker="item-title">Phone</a></div>
      <label data-marker="filters/localPriority/localPriority" role="switch"
             aria-checked="{str(initial).lower()}">
        Сначала из Саратова
        <input type="checkbox" data-marker="filters/localPriority/toggle"
               {"checked" if initial else ""}>
      </label>
      <script>
        window.changes = 0;
        const input = document.querySelector('input');
        input.addEventListener('change', () => {{
          window.changes++;
          input.closest('label').setAttribute('aria-checked', String(input.checked));
          location.hash = input.checked ? 'on' : 'off';
        }});
      </script>
    """)
    await search._apply_local_priority(page, requested)
    assert await search._local_priority_state(page) is requested
    assert await page.evaluate("window.changes") == int(initial != requested)
    # Re-applying the same setting must not turn it off or cause another reload.
    before_url = page.url
    await search._apply_local_priority(page, requested)
    assert page.url == before_url
    assert await page.evaluate("window.changes") == int(initial != requested)
    if initial != requested:
        assert page.url.endswith("#on" if requested else "#off")


@pytest.mark.asyncio
async def test_local_priority_rejects_unknown_or_conflicting_state(offline_page):
    page = offline_page
    await page.set_content("""
      <label data-marker="filters/localPriority/localPriority" role="switch"
             aria-checked="mixed">Сначала из Саратова
        <input type="checkbox" data-marker="filters/localPriority/toggle">
      </label>
    """)
    with pytest.raises(search.SearchDiscoveryError, match="unknown selection state"):
        await search._apply_local_priority(page, True)
    assert not await page.locator("input").is_checked()
    await page.locator("label").evaluate("el => el.setAttribute('aria-checked', 'true')")
    with pytest.raises(search.SearchDiscoveryError, match="disagree"):
        await search._apply_local_priority(page, True)
    assert not await page.locator("input").is_checked()
    await page.set_content("<div>No location controls</div>")
    with pytest.raises(search.SearchDiscoveryError, match="was not found"):
        await search._local_priority_state(page)


@pytest.mark.asyncio
async def test_local_priority_reports_setting_that_was_not_retained(monkeypatch):
    from unittest.mock import AsyncMock

    page = AsyncMock()
    monkeypatch.setattr(search, "_local_priority_state", AsyncMock(return_value=False))
    with pytest.raises(search.SearchDiscoveryError, match="did not retain"):
        await search._verify_local_priority(page, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["true", 1, [], {}])
async def test_local_priority_validation_prevents_any_navigation(invalid, monkeypatch):
    from unittest.mock import AsyncMock

    submit = AsyncMock()
    monkeypatch.setattr(search, "_submit_search_form", submit)
    with pytest.raises(search.SearchDiscoveryError, match="must be a boolean"):
        await search.search_avito(None, "https://www.avito.ru", "Phone", local_priority=invalid)
    submit.assert_not_awaited()
