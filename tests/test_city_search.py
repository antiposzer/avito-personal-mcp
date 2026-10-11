from __future__ import annotations

from typing import Any

import pytest

from avito_personal_mcp import search
from avito_personal_mcp.search import (
    CITY_INPUT_SELECTOR,
    CITY_OPTIONS_SELECTOR,
    LOCATION_BUTTON_SELECTOR,
    LOCATION_SAVE_SELECTOR,
    SearchDiscoveryError,
    _apply_city_filter,
    _location_title_matches,
    _matching_city_options,
    validate_city,
)


def test_validate_city_preserves_optional_and_normalizes_names() -> None:
    assert validate_city(None) is None
    assert validate_city("  Нижний\t Новгород  ") == "Нижний Новгород"
    assert validate_city(" Саратов,  Саратовская область ") == (
        "Саратов, Саратовская область"
    )


@pytest.mark.parametrize("city", ["  ", True, ","])
def test_validate_city_rejects_missing_city_name(city: object) -> None:
    with pytest.raises(SearchDiscoveryError, match="city must be a non-empty city name"):
        validate_city(city)  # type: ignore[arg-type]


def test_city_matching_uses_whole_city_name_and_normalized_spaces() -> None:
    labels = [
        "Саратовский район,Саратовская область",
        " Саратов,Саратовская область ",
        "Саратовская область",
        "Нижний  Новгород,Нижегородская область",
    ]
    assert _matching_city_options(labels, "саратов") == [1]
    assert _matching_city_options(labels, "Нижний Новгород") == [3]


def test_qualified_city_matching_resolves_namesakes_without_substring_matching() -> None:
    labels = [
        "Киров,Кировская область",
        "Киров,Калужская область",
        "Кировск,Мурманская область",
    ]
    assert _matching_city_options(labels, "Киров") == [0, 1]
    assert _matching_city_options(labels, " киров , Калужская область ") == [1]
    assert _matching_city_options(labels, "Киров,Мурманская область") == []


def test_location_title_accepts_only_observed_city_formats() -> None:
    assert _location_title_matches("Саратов", "Саратов")
    assert _location_title_matches("Саратов, район", "Саратов")
    assert _location_title_matches(" саратов , РАЙОН ", "Саратов, Саратовская область")
    for title in ["Москва", "Саратов,Москва", "Саратов,неизвестно", "Саратовская область", None]:
        assert not _location_title_matches(title, "Саратов")


class CityPickerPage:
    """Model the observed checkbox suggestions and saved location title."""

    def __init__(
        self,
        previous_city: str = "Москва",
        batches: list[list[str]] | None = None,
        retain_selection: bool = True,
        selected_index: int | None = None,
    ) -> None:
        self.title = previous_city
        self.url = "https://www.avito.ru/moskva/noutbuki"
        self.batches = batches or [
            [],
            [
                "Саратовский район,Саратовская область",
                "Саратов,Саратовская область",
            ],
        ]
        self.batch_index = 0
        self.labels: list[str] = []
        self.popup_open = False
        self.selected_index = selected_index
        self.retain_selection = retain_selection
        self.fills: list[str] = []
        self.clicks: list[tuple[str, int | None]] = []
        self.states: list[tuple[str, str]] = []
        self.waits: list[int] = []

    def locator(self, selector: str) -> CityLocator:
        assert selector in {
            LOCATION_BUTTON_SELECTOR,
            CITY_INPUT_SELECTOR,
            CITY_OPTIONS_SELECTOR,
            LOCATION_SAVE_SELECTOR,
        }
        return CityLocator(self, selector)

    async def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)


class CityLocator:
    def __init__(
        self, page: CityPickerPage, selector: str, index: int | None = None
    ) -> None:
        self.page = page
        self.selector = selector
        self.index = index

    @property
    def first(self) -> CityLocator:
        return self

    def nth(self, index: int) -> CityLocator:
        assert self.selector == CITY_OPTIONS_SELECTOR
        return CityLocator(self.page, self.selector, index)

    async def count(self) -> int:
        return 1

    async def get_attribute(self, name: str) -> str:
        if self.selector == LOCATION_BUTTON_SELECTOR:
            assert name == "title"
            return self.page.title
        assert self.selector == CITY_OPTIONS_SELECTOR and name == "aria-checked"
        assert self.index is not None
        return "true" if self.page.selected_index == self.index else "false"

    async def click(self, **_: object) -> None:
        self.page.clicks.append((self.selector, self.index))
        if self.selector == LOCATION_BUTTON_SELECTOR:
            self.page.popup_open = True
        elif self.selector == CITY_OPTIONS_SELECTOR:
            assert self.page.popup_open and self.index is not None
            self.page.selected_index = (
                None if self.page.selected_index == self.index else self.index
            )
        else:
            assert self.selector == LOCATION_SAVE_SELECTOR
            assert self.page.popup_open and self.page.selected_index is not None
            selected = self.page.labels[self.page.selected_index].split(",", 1)[0]
            if self.page.title != selected:
                self.page.url = "https://www.avito.ru/saratov/noutbuki"
            if self.page.retain_selection:
                self.page.title = selected
            self.page.popup_open = False

    async def wait_for(self, *, state: str, **_: object) -> None:
        self.page.states.append((self.selector, state))
        assert self.page.popup_open == (state == "visible")

    async def fill(self, value: str) -> None:
        assert self.selector == CITY_INPUT_SELECTOR and self.page.popup_open
        self.page.fills.append(value)

    async def all_text_contents(self) -> list[str]:
        assert self.selector == CITY_OPTIONS_SELECTOR
        self.page.labels = self.page.batches[
            min(self.page.batch_index, len(self.page.batches) - 1)
        ]
        self.page.batch_index += 1
        return self.page.labels

    async def inner_text(self) -> str:
        assert self.index is not None
        return self.page.labels[self.index]


@pytest.fixture
def observed_city_waits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    waits: list[str] = []

    async def refresh(page: CityPickerPage, before_url: str, operation: str) -> None:
        assert not page.popup_open
        assert before_url == "https://www.avito.ru/moskva/noutbuki"
        assert operation == "city selection"
        waits.append("refresh")

    async def hydrate(page: CityPickerPage, operation: str) -> None:
        assert not page.popup_open
        assert operation == "city selection"
        waits.append("hydrate")

    monkeypatch.setattr(search, "_wait_for_serp_refresh", refresh)
    monkeypatch.setattr(search, "_wait_for_hydrated_results", hydrate)
    return waits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_city", "city", "expected_wait"),
    [
        ("Москва", "Саратов", "refresh"),
        ("Саратов", "Саратов", "hydrate"),
        ("Саратов", "Саратов, Саратовская область", "refresh"),
    ],
)
async def test_city_picker_selects_exact_suggestion_and_waits_after_save(
    previous_city: str,
    city: str,
    expected_wait: str,
    observed_city_waits: list[str],
) -> None:
    page = CityPickerPage(previous_city=previous_city)

    await _apply_city_filter(page, city)

    assert page.fills == ["", city.split(",", 1)[0]]
    assert page.clicks == [
        (LOCATION_BUTTON_SELECTOR, None),
        (CITY_OPTIONS_SELECTOR, 1),
        (LOCATION_SAVE_SELECTOR, None),
    ]
    assert (CITY_INPUT_SELECTOR, "visible") in page.states
    assert (CITY_INPUT_SELECTOR, "hidden") in page.states
    assert page.batch_index >= 2  # The initial suggestions were not ready.
    assert page.title == "Саратов"
    assert observed_city_waits == [expected_wait]


@pytest.mark.asyncio
async def test_city_picker_saves_checked_city_without_toggling_it_off(
    observed_city_waits: list[str],
) -> None:
    page = CityPickerPage(previous_city="Саратов", selected_index=1)

    await _apply_city_filter(page, "Саратов")

    assert page.clicks == [
        (LOCATION_BUTTON_SELECTOR, None),
        (LOCATION_SAVE_SELECTOR, None),
    ]
    assert page.selected_index == 1
    assert page.title == "Саратов"
    assert observed_city_waits == ["hydrate"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (["Саратовский район,Саратовская область"], "exact city suggestion"),
        (
            ["Саратов,Саратовская область", "Саратов,Другая область"],
            "city is ambiguous",
        ),
    ],
)
async def test_city_picker_rejects_missing_or_ambiguous_suggestion_before_save(
    labels: list[str], message: str
) -> None:
    page = CityPickerPage(batches=[labels])

    with pytest.raises(SearchDiscoveryError, match=message):
        await _apply_city_filter(page, "Саратов")

    assert page.clicks == [(LOCATION_BUTTON_SELECTOR, None)]
    assert page.selected_index is None


@pytest.mark.asyncio
async def test_city_picker_rejects_suggestions_that_become_ambiguous_before_selection() -> None:
    page = CityPickerPage(
        batches=[
            ["Саратов,Саратовская область"],
            ["Саратов,Саратовская область", "Саратов,Другая область"],
        ]
    )

    with pytest.raises(SearchDiscoveryError, match="suggestions changed before selection"):
        await _apply_city_filter(page, "Саратов")

    assert page.clicks == [(LOCATION_BUTTON_SELECTOR, None)]
    assert page.selected_index is None
    assert page.title == "Москва"


@pytest.mark.asyncio
async def test_city_verification_waits_for_delayed_location_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = CityPickerPage(previous_city="Москва")

    async def delayed_title_update(timeout: int) -> None:
        assert page.title == "Москва"
        page.waits.append(timeout)
        if len(page.waits) == 2:
            page.title = "Саратов"

    monkeypatch.setattr(page, "wait_for_timeout", delayed_title_update)

    await search._verify_city_filter(page, "Саратов, Саратовская область")

    assert page.waits == [500, 500]
    assert page.title == "Саратов"
    assert page.clicks == []


@pytest.mark.asyncio
async def test_city_picker_rejects_saved_selection_when_title_did_not_change(
    observed_city_waits: list[str],
) -> None:
    page = CityPickerPage(retain_selection=False)

    with pytest.raises(SearchDiscoveryError, match="did not retain the requested city"):
        await _apply_city_filter(page, "Саратов")

    assert page.clicks[-1] == (LOCATION_SAVE_SELECTOR, None)
    assert observed_city_waits == ["refresh"]


@pytest.mark.asyncio
@pytest.mark.parametrize("city", [None, "  Саратов  "])
@pytest.mark.parametrize("local_priority", [None, True, False])
async def test_search_preserves_existing_options_and_verifies_city_after_all_filters(
    city: str | None, local_priority: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    page = CityPickerPage()

    async def submit(actual_page: object, origin: str, query: str) -> None:
        assert actual_page is page
        assert (origin, query) == ("https://www.avito.ru", "ноутбук")
        events.append("submit")

    async def apply_city(actual_page: CityPickerPage, actual_city: str) -> None:
        assert actual_page is page and actual_city == "Саратов"
        page.title = actual_city
        events.append("city")

    async def price(actual_page: object, minimum: int, maximum: int) -> None:
        assert actual_page is page
        events.append(("price", minimum, maximum))

    async def sort(actual_page: object, value: str) -> None:
        assert actual_page is page
        events.append(("sort", value))

    original_verify = search._verify_city_filter

    async def verify(actual_page: CityPickerPage, actual_city: str) -> None:
        await original_verify(actual_page, actual_city)
        events.append("verify-city")

    async def evaluate(_: str, limit: int) -> dict[str, Any]:
        events.append(("read", limit))
        return {
            "hasSerp": True,
            "items": [{"href": "/item_1234567890", "title": "Ноутбук", "price": "150 ₽"}],
        }

    monkeypatch.setattr(search, "_submit_search_form", submit)
    monkeypatch.setattr(search, "_apply_city_filter", apply_city)
    monkeypatch.setattr(search, "_apply_price_filter", price)
    monkeypatch.setattr(search, "_apply_sort", sort)
    monkeypatch.setattr(search, "_verify_city_filter", verify)
    monkeypatch.setattr(page, "evaluate", evaluate, raising=False)

    async def priority(actual_page: object, value: bool) -> None:
        assert actual_page is page and value is local_priority
        events.append(("priority", value))

    async def verify_priority(actual_page: object, value: bool) -> None:
        assert actual_page is page and value is local_priority
        events.append(("verify-priority", value))

    monkeypatch.setattr(search, "_apply_local_priority", priority)
    monkeypatch.setattr(search, "_verify_local_priority", verify_priority)

    results = await search.search_avito(
        page, "https://www.avito.ru", " ноутбук ", 3, 100, 200, "price_asc", city, local_priority
    )

    expected: list[object] = ["submit"]
    if city is not None:
        expected.append("city")
    if local_priority is not None:
        expected.append(("priority", local_priority))
    expected.extend([("price", 100, 200), ("sort", "price_asc")])
    if city is not None:
        expected.append("verify-city")
    if local_priority is not None:
        expected.append(("verify-priority", local_priority))
    expected.append(("read", 3))
    assert events == expected
    assert results[0]["id"] == 1234567890
