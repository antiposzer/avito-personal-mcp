# Avito Personal MCP

Unofficial, local-first MCP server for **personal Avito accounts**. It lets an MCP-capable AI client work with your own Avito account through a dedicated browser session — **without requiring Avito Developer API credentials**.

> [!IMPORTANT]
> This project is independent and is not affiliated with, endorsed by, or maintained by Avito.

## Why this project exists

Many Avito integrations are built around official developer/business APIs or public listing parsing. Avito Personal MCP is aimed at a different use case: an ordinary user who wants an AI assistant to work with the Avito account they already use in the browser.

The user signs in to Avito manually in a dedicated Chrome profile. The MCP server then attaches to that already authenticated browser over Chrome DevTools Protocol (CDP).

**No Avito Developer `Client ID` or `Client Secret` is required.** The project also does **not** ask users to provide Avito passwords, SMS/OTP codes, cookies, authorization headers, session tokens, or exported browser storage state.

This makes the project suitable for personal-account workflows such as:

- searching Avito and inspecting individual listings;
- reviewing your own listings;
- reviewing your favorites;
- reading your Avito conversations and message history;
- preparing and explicitly confirming a message before it is sent.

It is not intended to bypass Avito authentication, CAPTCHA, anti-bot controls, account restrictions, or access controls.

## What it does

Avito Personal MCP exposes a local-first MCP interface over the user's own Avito web session. Authentication remains in the dedicated browser controlled by the user rather than being copied into MCP configuration.

## Current MCP tools

Read-only tools:

- `avito_selfcheck`
- `avito_me`
- `avito_search`
- `avito_get_listing`
- `avito_my_listings`
- `avito_favorites`
- `avito_chats`
- `avito_chat_messages`

Guarded write tools:

- `avito_send_message`

`avito_send_message` is deliberately two-step. The first call only returns a sanitized preview and a short-lived one-time confirmation token. A second call with the same chat id, the same message text, and that token performs one send attempt. The MCP does not automatically retry a send because a retry could create a duplicate message.

## Requirements

- Python 3.11, 3.12, 3.13, or 3.14
- Google Chrome or another compatible Chromium browser with CDP support
- a dedicated browser profile for this project
- manual Avito authentication by the user

## Installation

Clone the repository and install the package into a virtual environment:

```bash
git clone https://github.com/AlexButiev/avito-personal-mcp.git
cd avito-personal-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Run the MCP server over stdio:

```bash
avito-personal-mcp
```

The console entry point is installed by the package and does not require a repository-local `PYTHONPATH`.

## Dedicated Chrome session

Use a separate Chrome profile. Do not reuse a normal browser profile that also contains banking, email, work, or other sensitive accounts.

On macOS:

```bash
open -na "Google Chrome" --args \
  --user-data-dir="$HOME/.avito-personal-mcp/chrome-profile" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  "https://www.avito.ru"
```

Then sign in to Avito manually inside that Chrome window. Do not put Avito credentials, cookies, tokens, or browser storage in MCP client configuration.

### CDP security

Keep CDP bound to loopback only (`127.0.0.1`). Do not expose port `9222` to the LAN, Internet, a public tunnel, or an untrusted container/network namespace. A process that can reach the CDP endpoint can potentially control the attached browser session.

## MCP client configuration

The server uses stdio. A generic MCP client configuration looks like this:

```json
{
  "mcpServers": {
    "avito": {
      "command": "/absolute/path/to/.venv/bin/avito-personal-mcp"
    }
  }
}
```

Use the actual absolute path to the installed console script on your machine. No Avito credentials or session material belong in this configuration.

Chrome and the MCP server are separate processes: start the dedicated Chrome session first, sign in manually if needed, then let the MCP client launch `avito-personal-mcp`.

### Codex and ChatGPT desktop app

On one Mac, the ChatGPT desktop app, Codex CLI, and the Codex IDE extension can
share the same `~/.codex/config.toml` MCP configuration. Add the installed
`avito-personal-mcp` command there and restart the client; `/mcp` listing
`avito` verifies that the server can launch. It does not by itself make Avito
available in an ordinary ChatGPT conversation. For that experience, install
the matching local `Avito Personal` plugin, start a new ordinary chat, and
first confirm a safe `avito_selfcheck` call before relying on natural-language
requests.

Keep `avito_send_message` on prompt/approval and leave
`avito_chat_messages` on prompt unless you accept Avito's normal behaviour that
opening a conversation can mark it read. The configuration and the separate
private Secure MCP Tunnel route for ChatGPT on the web are documented in
[docs/CHATGPT_CONNECTION.md](docs/CHATGPT_CONNECTION.md).

## Terminal fallback: `avito-ai`

`avito-ai` is a small terminal client for the same local MCP surface. It is useful
when the AI client cannot yet attach to the MCP server directly, or when you want
to copy a compact, structured result into a chat.

```bash
avito-ai selfcheck
avito-ai --compact search "мини ПК для HomeLab" --limit 10
avito-ai --compact search "ноутбук" --min-price 10000 --max-price 100000 --sort price_asc --limit 10
avito-ai --compact search "iPhone 15 Pro Max" --city "Саратов" --limit 10
avito-ai --compact search "AirPods 5" --city "Саратов" --local-priority --limit 10
avito-ai listing OWN_LISTING_ID
avito-ai listing '<exact-Avito-URL-copied-from-browser-or-search-result>'
avito-ai messages CHAT_ID --limit 20
```

### Search city, price and sort

Search results also include `condition`, `location`, `rating`, and `badges`,
read directly from each result card without opening listings or profiles.
`condition` is the displayed condition/parameter text (for example, `Новый`
or `Б/у`), `location` is the displayed location (which may be only a district),
and `rating` is the displayed seller score as text (for example, `4,8`).
`badges` is a list of displayed labels, including price assessments, guarantees,
payment options, and seller badges. Missing text fields are `null`; absent
badges are `[]`. These are output fields, not additional search filters.

`avito_search` accepts optional `city`, `min_price`, `max_price`, and `sort` arguments;
the same options are exposed by `avito-ai search` as `--min-price`,
`--max-price`, `--sort`, and `--city`. Price bounds are non-negative integers and the
maximum cannot be below the minimum. Supported sort values are:

- `default` — Avito's normal result order;
- `price_asc` / `price_desc` — lower or higher price first;
- `date_desc` — newest first;
- `discount_desc` — larger discount first.

For example, `city="Саратов"` selects that city through Avito's location popup.
Suggestions must match the whole city name. If several regions contain that
name, specify the exact visible label, such as `city="Саратов, Саратовская область"`.
The server reports an error rather than selecting an ambiguous or missing city.
It verifies the location button's title after saving and again after other
filters. Without `city`, existing location behaviour is preserved. The city
parameter does not change the current radius or nearby-area settings.

`local_priority=true` turns on Avito's observed **Сначала из <город>** switch;
`false` turns it off. Omit the argument (or use `null`) to preserve the site's
setting. With `city`, priority is applied after selecting that city; without
`city`, it applies to the currently selected location. The equivalent CLI
flags are `--local-priority` and `--no-local-priority`. The server checks both
the switch's accessible state and its checkbox, waits for a result refresh
when changing the setting, and verifies it again after price/sort changes.
The requested setting is returned as `applied.local_priority`.
This is a preference for local results, not a strict city-only filter: other
cities can still appear in the result set.

The server uses Avito's rendered location picker, price inputs and sort menu, then verifies that
the visible result set refreshed. It does not construct undocumented search
URLs or pass arbitrary filter selectors from a client. Category-specific
filters, radius, and pagination are intentionally deferred until each has
its own stable UI observation and fail-closed acceptance.

A bare numeric ID is deliberately supported only for one of **your own**
listings: the server first finds its observed URL in the authenticated profile.
An exact public listing URL can be read without an authenticated header, provided
navigation stays on Avito and the resulting listing ID matches the requested ID.
Listing details read dates only from `item-view/item-date`. `date` preserves the
displayed text; `date_iso` resolves supported Russian labels using the saved
`date_observed_at` in `Europe/Saratov`. Date-only labels retain day precision
without an invented time. Unsupported labels keep their original text with a
null ISO value. If a displayed calendar date omits its year, the most recent
past year is inferred and marked with `date_year_inferred=true`. Windows uses
the packaged `tzdata` database rather than the host's implicit timezone.
For another public listing, pass its exact same-origin Avito URL, copied from the
browser or returned by `avito_search`. The server never guesses a listing URL
from a numeric ID, because Avito paths include volatile location/category/slug
segments and a guessed path is neither reliable nor safe.

The command launches the installed `avito-personal-mcp` console entry point from
the same Python environment, not a repository checkout or a private browser
helper. It therefore exercises the packaged MCP path while keeping Chrome, CDP,
and all Avito authentication material outside the CLI configuration. The bridge
currently exposes read-only commands only; guarded message sends remain available
only through the explicit two-phase MCP tool.

To use a different **loopback** CDP port, set the documented server setting for
the individual command; `avito-ai` forwards only this setting to its child MCP
server and does not forward other shell environment values:

```bash
AVITO_MCP_CDP_URL=http://127.0.0.1:9333 avito-ai selfcheck
```

## Architecture

```text
MCP client
    |
    v
Avito Personal MCP
    |
    +-- browser/session layer
    +-- profile discovery
    +-- search/listings
    +-- favorites
    +-- chats/messages
    +-- guarded confirmations for writes
    +-- diagnostics
    |
    v
Dedicated user-controlled Chrome session (CDP on loopback)
    |
    v
avito.ru
```

The implementation prefers browser-visible page state and frontend behavior observed in the user's own authenticated session. It does not guess private endpoints and does not attempt to bypass CAPTCHA or anti-bot controls.

## Behavioral caveats

Opening an Avito conversation can cause Avito itself to mark that conversation as read. `avito_chat_messages` does not intentionally send, edit, delete, react to, or otherwise mutate messages, but normal page navigation can still affect read state.

Browser-driven operations are inherently coupled to Avito's current DOM. The project therefore treats unexpected DOM changes as errors rather than silently pretending an empty result is valid.

## Safety principles

- Local-first authentication/session state.
- No Avito Developer API credentials required for the current browser-session workflow.
- No Avito credentials committed to the repository.
- No CAPTCHA bypass, stealth circumvention, or automated OTP handling.
- CDP stays on loopback only.
- Dedicated Chrome profile only; do not use it for unrelated sensitive accounts.
- Read operations are separated from write operations.
- Write operations require explicit confirmation safeguards.
- Message sends are never automatically retried after an irreversible click.
- Logs/tests must not contain passwords, cookies, authorization headers, session tokens, browser storage state, or real private-message fixtures.

See [SECURITY.md](SECURITY.md) for the security policy.

## Troubleshooting

### `chrome_unreachable`

Confirm the dedicated Chrome instance is running with `--remote-debugging-address=127.0.0.1 --remote-debugging-port=9222` and that no firewall/container boundary prevents the local MCP process from reaching it.

### `no_avito_tab`

Open `https://www.avito.ru` in the dedicated Chrome window.

### Authentication unavailable / login redirect

Sign in manually in the dedicated Chrome window. Do not paste credentials, cookies, or tokens into MCP configuration.

### DOM mismatch / unavailable data

Avito may have changed its frontend. Fail closed and update selectors only after observing the new browser behavior. Do not guess internal APIs or weaken authentication checks.

## Development

Install development dependencies:

```bash
python -m pip install -e '.[dev]'
```

Run local checks:

```bash
pytest
ruff check .
```

GitHub Actions runs sanitized unit tests and Ruff on Python 3.11, 3.12, 3.13, and 3.14 without Avito credentials or a real browser session. Live CDP acceptance is intentionally separate from CI.

## Project status

`0.1.0rc3` is the current public release candidate. It adds the packaged
`avito-ai` fallback, data-minimized diagnostics, Gate 12 acceptance fixes, and
the first stable price/sort search controls to the read-only foundation and
guarded message-send path. Browser-driven integrations can still break when
Avito changes its frontend, so treat the release candidate as experimental until
the final release is promoted.

See [CHANGELOG.md](CHANGELOG.md) for release notes and
[docs/ROADMAP.md](docs/ROADMAP.md) for the capability boundary, safety model,
and planned development order. See
[docs/CHATGPT_CONNECTION.md](docs/CHATGPT_CONNECTION.md) for the local desktop
connection and the private-tunnel runbook for ChatGPT on the web.

## License

MIT. See [LICENSE](LICENSE).

### Universal listing monitoring

A local five-minute monitor supports city, condition, inferred seller type, minimum account rating and maximum listing age. Call `avito_monitor_panel` for its local dashboard, or run `avito-ai monitor-panel` and keep the process alive. See [MONITORING.md](docs/MONITORING.md) for accuracy limits, persistence and the event feed for a future notification bot.
