"""Read-only discovery of one Avito listing from the rendered listing page."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from avito_personal_mcp.listing_date import now_in_saratov, parse_listing_date
from avito_personal_mcp.listings import discover_own_listings
from avito_personal_mcp.navigation import (
    canonical_same_origin_url,
    has_authenticated_marker,
)

LISTING_ID_RE = re.compile(r"(?:^|_)(\d{4,})(?:$|[/?#])")


class ListingDetailError(RuntimeError):
    """Raised when a listing detail page cannot be resolved or parsed safely."""


def extract_listing_id(value: int | str) -> int:
    """Extract a numeric Avito listing id from an integer, numeric string, or URL."""

    if isinstance(value, int):
        if value <= 0:
            raise ListingDetailError("Listing id must be positive")
        return value

    if not isinstance(value, str):
        raise ListingDetailError("Listing reference must be an id or Avito URL")

    text = value.strip()
    if text.isdigit():
        return int(text)

    match = LISTING_ID_RE.search(urlparse(text).path)
    if not match:
        raise ListingDetailError("Could not extract listing id from the supplied reference")
    return int(match.group(1))


async def resolve_listing_url(page: Page, origin: str, reference: int | str) -> tuple[int, str]:
    """Resolve a canonical own-listing URL without guessing Avito endpoint paths."""

    listing_id = extract_listing_id(reference)

    if isinstance(reference, str):
        text = reference.strip()
        if "://" in text:
            try:
                return listing_id, canonical_same_origin_url(text, origin)
            except ValueError as exc:
                raise ListingDetailError(str(exc)) from exc

    listings = await discover_own_listings(page, origin)
    for listing in listings:
        if listing.get("id") == listing_id and isinstance(listing.get("url"), str):
            try:
                safe_url = canonical_same_origin_url(listing["url"], origin)
            except ValueError as exc:
                raise ListingDetailError(
                    "Resolved listing URL is outside the Avito origin"
                ) from exc
            return listing_id, safe_url

    raise ListingDetailError(
        "Listing id was not found among the authenticated user's listings. "
        "Pass an exact Avito URL for another public listing; URLs are never guessed from IDs."
    )


def normalize_detail(
    raw: dict[str, Any], listing_id: int, url: str, observed_at: datetime | None = None
) -> dict[str, Any]:
    """Normalize safe listing metadata collected from stable DOM markers."""

    def clean(name: str) -> str | None:
        value = raw.get(name)
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())
        return value or None

    params = raw.get("params")
    if not isinstance(params, list):
        params = []

    normalized_params: list[str] = []
    for item in params:
        if not isinstance(item, str):
            continue
        value = " ".join(item.split())
        if not value or value.endswith(":"):
            continue
        if value not in normalized_params:
            normalized_params.append(value)

    description = raw.get("description")
    if isinstance(description, str):
        description = "\n".join(" ".join(line.split()) for line in description.splitlines())
        description = re.sub(r"\n{3,}", "\n\n", description).strip() or None
    else:
        description = None

    state = "inactive" if raw.get("expired") or raw.get("can_activate") else "active_or_unknown"

    return {
        "id": listing_id,
        "url": url,
        "title": clean("title"),
        "price": clean("price"),
        "description": description,
        "params": normalized_params,
        "seller_name": clean("seller_name"),
        "state": state,
        **parse_listing_date(raw.get("date"), observed_at),
        "date_source": "item-view/item-date" if raw.get("date") else None,
    }


async def discover_listing_detail(
    page: Page,
    origin: str,
    reference: int | str,
) -> dict[str, Any]:
    """Read one listing page using only observed stable ``data-marker`` attributes."""

    listing_id, url = await resolve_listing_url(page, origin, reference)
    if page.url.split("?", 1)[0] != url:
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            raise ListingDetailError(f"Listing page returned HTTP {response.status}")

    await page.wait_for_timeout(750)

    # Exact public URLs can be read without an authenticated header. Bare IDs
    # still require the own-listing lookup and authenticated-page checks.
    explicit_public_url = isinstance(reference, str) and "://" in reference.strip()
    if not explicit_public_url and not await has_authenticated_marker(page):
        raise ListingDetailError(
            "Avito authentication is unavailable or the browser session has expired"
        )

    try:
        actual_url = canonical_same_origin_url(page.url, origin)
        actual_id = extract_listing_id(actual_url)
    except (ValueError, ListingDetailError) as exc:
        raise ListingDetailError("Navigation did not reach the requested Avito listing") from exc
    if actual_id != listing_id:
        raise ListingDetailError("Navigation reached a different Avito listing")

    # Closed listings can retain their description behind this observed,
    # read-only control. Expand it only when its visible label matches.
    expand = page.locator('[data-marker="item-view/close-extended-button"]')
    if await expand.count() == 1 and await expand.is_visible():
        if (await expand.inner_text()).strip() == "Показать описание":
            await expand.click(timeout=5_000)

    # The title can render before the asynchronously populated description.
    # Wait for its actual content, while allowing genuinely absent descriptions.
    try:
        await page.wait_for_function(
            """() => {
                const el = document.querySelector('[data-marker="item-view/item-description"]');
                return Boolean(el && (el.innerText || el.textContent || '').trim());
            }""",
            timeout=5_000,
        )
    except PlaywrightTimeoutError:
        pass

    observed_at = now_in_saratov()
    raw = await page.evaluate(
        """
        () => {
            const text = (selector) => {
                const el = document.querySelector(selector);
                return el ? (el.textContent || '').trim() || null : null;
            };

            const paramsRoot = document.querySelector('[data-marker="item-view/item-params"]');
            const params = paramsRoot
                ? [...paramsRoot.querySelectorAll('li, p, span, div')]
                    .map(el => (el.textContent || '').trim())
                    .filter(Boolean)
                    .filter((value, index, array) => array.indexOf(value) === index)
                    .filter(value => value.length <= 300)
                : [];

            const titleMarker = [
                '[data-marker="item-view/title-info"]',
                '[data-marker="item-view-seller/title-info"]'
            ].join(', ');
            const descriptionEl = document.querySelector(
                '[data-marker="item-view/item-description"]'
            );

            return {
                title: text('[data-marker="item-view/title-info"]')
                    || text('[data-marker="item-view-seller/title-info"]'),
                price: text('[data-marker="item-view/item-price"]')
                    || text('[data-marker="item-view-seller/item-price"]'),
                description: descriptionEl
                    ? (descriptionEl.innerText || descriptionEl.textContent || '').trim() || null
                    : null,
                params,
                seller_name: text('[data-marker="seller-info/name"]'),
                date: text('[data-marker="item-view/item-date"]'),
                displayed_id: text('[data-marker="item-view/item-id"]'),
                expired: Boolean(
                    document.querySelector(
                        '[data-marker="expired-item-note"], '
                        + '[data-marker="item-view/closed-warning"]'
                    )
                ),
                can_activate: Boolean(
                    document.querySelector('[data-marker="activate-item-button"]')
                ),
                has_title_marker: Boolean(document.querySelector(titleMarker)),
            };
        }
        """
    )

    if not isinstance(raw, dict):
        raise ListingDetailError("Avito listing page returned an unexpected DOM result")
    if not raw.get("has_title_marker"):
        raise ListingDetailError(
            "Avito listing page structure did not match the observed listing DOM"
        )

    displayed_id = raw.get("displayed_id")
    if isinstance(displayed_id, str):
        match = re.fullmatch(r"№?\s*(\d+)", displayed_id.strip())
        if match is None or int(match[1]) != listing_id:
            raise ListingDetailError("Avito displayed a different listing id")

    return normalize_detail(raw, listing_id, url, observed_at)
