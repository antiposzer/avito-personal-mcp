# Local city-filter acceptance

Date: 2026-10-05. This records a working-tree extension, not a new public release.

## Observations

The user supplied DOM fragments for the normal location-selection workflow:

- Open: `search-form/change-location`, containing an anchor with `role="button"` and a location `title`.
- Search input: `popup-location/region/search-input`.
- Suggestions: buttons with `role="checkbox"`, `aria-checked`, and marker prefix `popup-location/region/custom-option(`.
- A suggestion contains the city and its region separated by a comma. Its index marker is not used to choose a city.
- Apply: `popup-location/save-button`.

Live MCP acceptance additionally observed a SERP location title in the form
`<city>, район`. Verification accepts this observed district-picker caption and
the plain city title; other suffixes or lists of cities are not accepted.

## Behaviour

- `avito_search` accepts optional `city`; `avito-ai search` accepts `--city`.
- Exact whole-name matches are required. Missing and ambiguous suggestions fail
  before applying the selection. A city can be qualified by its visible region.
- An already checked suggestion is not toggled off.
- The popup must close, results must be hydrated/refreshed, and the location
  title must confirm the city. Title verification allows bounded UI latency and
  runs again after price/sort filters.
- Without `city`, existing location behaviour is unchanged.

## Checks

- Automated coverage: input validation, exact and qualified names, ambiguous and
  missing suggestions, changing suggestions, checked-state handling, delayed and
  incorrect titles, search orchestration, MCP/CLI forwarding and failure cleanup.
- Live search through the installed stdio MCP CLI passed for the requested city.
- Changing to an adjacent city also passed; the returned listing URL matched
  that city. The requested city is restored after the check.
- The CLI imports this checkout through its existing editable installation.

No session data, browser storage, profile information, public listing contents or
raw DOM dumps are included in this acceptance record.

## Scope

This validates the observed desktop city picker. Region-qualified names and
ambiguous cities have unit coverage but were not exercised live. Radius,
nearby-area settings, private-seller filtering and pagination are outside this
extension. Changing the city does not guarantee every recommended/promoted card
is strictly inside the city boundary. Existing MCP clients must reload the
server/tool schema before using the new argument.
