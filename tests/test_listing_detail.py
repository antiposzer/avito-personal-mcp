from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from avito_personal_mcp import listing_detail
from avito_personal_mcp.listing_date import SARATOV
from avito_personal_mcp.listing_detail import (
    ListingDetailError,
    extract_listing_id,
    normalize_detail,
    resolve_listing_url,
)


def test_extract_listing_id_from_integer():
    assert extract_listing_id(7873932069) == 7873932069


def test_extract_listing_id_from_numeric_string():
    assert extract_listing_id("7873932069") == 7873932069


def test_extract_listing_id_from_url():
    url = "https://www.avito.ru/city/category/example_7873932069"
    assert extract_listing_id(url) == 7873932069


def test_extract_listing_id_rejects_invalid_reference():
    with pytest.raises(ListingDetailError, match="Could not extract"):
        extract_listing_id("https://www.avito.ru/profile")


def test_normalize_detail():
    raw = {
        "title": "  Example listing  ",
        "price": "1 200 ₽",
        "description": "  Example   description ",
        "params": ["Condition: used", "  Brand: Example  "],
        "seller_name": " Seller ",
        "expired": True,
        "can_activate": True,
    }

    reference = datetime(2026, 10, 6, 22, 30, tzinfo=SARATOV)
    assert normalize_detail(raw, 1234567890, "https://www.avito.ru/x_1234567890", reference) == {
        "id": 1234567890,
        "url": "https://www.avito.ru/x_1234567890",
        "title": "Example listing",
        "price": "1 200 ₽",
        "description": "Example description",
        "params": ["Condition: used", "Brand: Example"],
        "seller_name": "Seller",
        "state": "inactive",
        "date": None,
        "date_iso": None,
        "date_precision": None,
        "date_observed_at": "2026-10-06T22:30:00+04:00",
        "date_timezone": "Europe/Saratov",
        "date_year_inferred": False,
        "date_parse_status": "missing",
        "date_source": None,
    }


def test_normalize_description_preserves_paragraphs_and_full_text():
    result = normalize_detail(
        {"description": "  First   paragraph.\n\n\nSecond\tparagraph.  \n"},
        1234567890,
        "https://www.avito.ru/item_1234567890",
    )
    assert result["description"] == "First paragraph.\n\nSecond paragraph."


async def test_public_detail_does_not_require_authenticated_header(monkeypatch):
    url = "https://www.avito.ru/saratov/telefony/phone_1234567890"
    page = AsyncMock()
    page.url = url
    page.locator = Mock(return_value=AsyncMock(count=AsyncMock(return_value=0)))
    page.evaluate.return_value = {
        "has_title_marker": True,
        "title": "Phone",
        "description": "Full public description",
        "params": [],
    }
    auth = AsyncMock(return_value=False)
    monkeypatch.setattr(listing_detail, "has_authenticated_marker", auth)
    result = await listing_detail.discover_listing_detail(page, "https://www.avito.ru", url)
    assert result["description"] == "Full public description"
    assert result["id"] == 1234567890
    auth.assert_not_awaited()


async def test_bare_id_detail_still_requires_authenticated_session(monkeypatch):
    url = "https://www.avito.ru/saratov/telefony/phone_1234567890"
    page = AsyncMock()
    page.url = url
    monkeypatch.setattr(
        listing_detail,
        "discover_own_listings",
        AsyncMock(return_value=[{"id": 1234567890, "url": url}]),
    )
    monkeypatch.setattr(listing_detail, "has_authenticated_marker", AsyncMock(return_value=False))
    with pytest.raises(ListingDetailError, match="authentication is unavailable"):
        await listing_detail.discover_listing_detail(page, "https://www.avito.ru", 1234567890)
    page.evaluate.assert_not_awaited()


@pytest.mark.parametrize(
    "redirect",
    [
        "https://example.test/phone_1234567890",
        "https://www.avito.ru/profile/login",
        "https://www.avito.ru/saratov/telefony/phone_1234567891",
    ],
)
async def test_public_detail_rejects_off_origin_or_wrong_listing_redirect(redirect):
    url = "https://www.avito.ru/saratov/telefony/phone_1234567890"
    page = AsyncMock()
    page.url = redirect
    page.goto.return_value = None
    with pytest.raises(ListingDetailError, match="Navigation"):
        await listing_detail.discover_listing_detail(page, "https://www.avito.ru", url)
    page.evaluate.assert_not_awaited()


async def test_resolve_listing_url_uses_exact_public_url_without_own_listing_lookup(
    monkeypatch: pytest.MonkeyPatch,
):
    async def unexpected_lookup(*_args, **_kwargs):
        raise AssertionError("An explicit listing URL must not inspect own listings")

    monkeypatch.setattr(
        "avito_personal_mcp.listing_detail.discover_own_listings", unexpected_lookup
    )

    assert await resolve_listing_url(
        None,  # type: ignore[arg-type]
        "https://www.avito.ru",
        "https://www.avito.ru/city/category/exact_listing_1234567890?context=search",
    ) == (1234567890, "https://www.avito.ru/city/category/exact_listing_1234567890")


async def test_resolve_listing_url_resolves_bare_id_only_from_own_listings(
    monkeypatch: pytest.MonkeyPatch,
):
    async def own_listings(*_args, **_kwargs):
        return [{"id": 1234567890, "url": "https://www.avito.ru/own_1234567890"}]

    monkeypatch.setattr("avito_personal_mcp.listing_detail.discover_own_listings", own_listings)

    assert await resolve_listing_url(None, "https://www.avito.ru", 1234567890) == (
        1234567890,
        "https://www.avito.ru/own_1234567890",
    )


async def test_resolve_listing_url_rejects_unknown_bare_id_without_guessing_url(
    monkeypatch: pytest.MonkeyPatch,
):
    async def no_own_listings(*_args, **_kwargs):
        return []

    monkeypatch.setattr("avito_personal_mcp.listing_detail.discover_own_listings", no_own_listings)

    with pytest.raises(ListingDetailError, match="URLs are never guessed from IDs"):
        await resolve_listing_url(None, "https://www.avito.ru", "1234567890")
