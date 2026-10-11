"""Verify message metadata and delayed hydration using sanitized, offline DOM."""

import pytest
from playwright.async_api import Error, async_playwright

from avito_personal_mcp import chat_messages
from avito_personal_mcp.chat_messages import discover_chat_messages, normalize_message


def test_multiline_message_keeps_lines_and_normalizes_spacing_within_each_line():
    result = normalize_message(
        {
            "direction_class": "right-xx",
            "text": "  First\u00a0  line \r\n\tSecond  line  ",
            "datetime": "2026-10-07T22:13:38",
        }
    )

    assert result == {
        "direction": "outgoing",
        "type": "text",
        "text": "First line\nSecond line",
        "datetime": "2026-10-07T22:13:38",
        "attachments": [],
    }


@pytest.mark.parametrize(
    ("direction_class", "direction"),
    [
        ("left-xx", "incoming"),
        ("right-xx", "outgoing"),
        ("message-base-module-left-xx", "incoming"),
        ("message-base-module-right-xx", "outgoing"),
        (None, "unknown"),
    ],
)
def test_ordinary_messages_retain_direction_without_fabricating_attachments(
    direction_class, direction
):
    result = normalize_message({"direction_class": direction_class, "text": "Hello"})

    assert result["direction"] == direction
    assert result["type"] == "text"
    assert result["attachments"] == []


def test_file_metadata_is_available_without_message_text_or_file_download():
    result = normalize_message(
        {
            "direction_class": "left-xx",
            "text": None,
            "attachments": [{"name": "receipt.png", "size": "10 КБ · PNG"}],
            "datetime": "2026-10-07T22:13:38",
        }
    )

    assert result == {
        "direction": "incoming",
        "type": "file",
        "text": None,
        "datetime": "2026-10-07T22:13:38",
        "attachments": [{"name": "receipt.png", "size": "10 КБ · PNG"}],
    }


def test_message_text_and_file_metadata_are_both_preserved():
    result = normalize_message(
        {
            "direction_class": "right-xx",
            "text": "  Here is the file  ",
            "attachments": [{"name": "receipt.png", "size": "10 КБ · PNG"}],
        }
    )

    assert result["type"] == "text"
    assert result["text"] == "Here is the file"
    assert result["attachments"] == [{"name": "receipt.png", "size": "10 КБ · PNG"}]


def test_platform_message_is_not_misattributed_to_the_other_person():
    result = normalize_message(
        {
            "direction_class": "left-xx",
            "is_system": True,
            "text": "  Platform\u00a0notification \n  Second line  ",
            "datetime": "2026-10-07T22:13:38",
        }
    )

    assert result == {
        "direction": "system",
        "type": "system",
        "text": "Platform notification\nSecond line",
        "datetime": "2026-10-07T22:13:38",
        "attachments": [],
    }


@pytest.mark.parametrize(("has_image", "expected_type"), [(True, "image"), (False, "other")])
def test_enrichment_keeps_non_text_messages_without_inventing_content(has_image, expected_type):
    result = normalize_message({"has_image": has_image})

    assert result["type"] == expected_type
    assert result["text"] is None
    assert result["datetime"] is None
    assert result["attachments"] == []


@pytest.mark.asyncio
async def test_last_ten_messages_wait_for_dates_and_preserve_observed_file_and_system_dom(
    monkeypatch,
):
    async def authenticated(_page):
        return True

    monkeypatch.setattr(chat_messages, "has_authenticated_marker", authenticated)

    # These are synthetic fixtures, not private chat data. No authenticated
    # profile, MCP call, CDP session, image fetch or file download is used.
    origin = "https://avito.invalid"
    target = origin + "/profile/messenger/channel/chat-test"
    ordinary_messages = "".join(
        f'<div data-marker="message" class="root-xx left-xx">'
        f'<span data-marker="messageText">Message {index}</span></div>'
        for index in range(9)
    )
    html = ordinary_messages + """
      <div data-marker="message" class="root-xx right-xx">
        <span data-marker="messageText">  First   line
  Second\u00a0line  </span>
      </div>
      <div data-marker="message" class="root-xx left-xx">
        <div data-marker="messageFile">
          <div data-marker="messageFile/name">receipt.png</div>
          <div data-marker="messageFile/fileSize">10 КБ · PNG</div>
        </div>
      </div>
      <div data-marker="message" class="root-xx left-xx">
        <span data-marker="platformMessage/text">Platform notification</span>
      </div>
    """
    body = (
        '<div data-marker="desktop-messenger">'
        '<div data-marker="messagesHistory/list">'
        + html
        + """</div></div>
        <script>
          window.hydrated = false;
          setTimeout(() => {
            document.querySelectorAll('[data-marker="message"]').forEach((message, index) => {
              const time = document.createElement('time');
              time.setAttribute('datetime', `2026-10-07T22:13:${28 + index}`);
              message.appendChild(time);
            });
            window.hydrated = true;
          }, 1300);
        </script>"""
    )

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
            routed_requests = []

            async def fulfill_synthetic_page(route):
                routed_requests.append(route.request.url)
                if route.request.url == target:
                    await route.fulfill(content_type="text/html; charset=utf-8", body=body)
                else:
                    await route.abort()

            await page.route("**/*", fulfill_synthetic_page)
            results = await discover_chat_messages(page, origin, "chat-test", limit=10)

            assert page.url == target
            assert await page.evaluate("window.hydrated") is True
            assert routed_requests == [target]
            assert len(results) == 10
            assert [r["text"] for r in results[:7]] == [
                f"Message {index}" for index in range(2, 9)
            ]
            assert [r["datetime"] for r in results] == [
                f"2026-10-07T22:13:{second}" for second in range(30, 40)
            ]
            assert results[-3] == {
                "direction": "outgoing",
                "type": "text",
                "text": "First line\nSecond line",
                "datetime": "2026-10-07T22:13:37",
                "attachments": [],
            }
            assert results[-2] == {
                "direction": "incoming",
                "type": "file",
                "text": None,
                "datetime": "2026-10-07T22:13:38",
                "attachments": [{"name": "receipt.png", "size": "10 КБ · PNG"}],
            }
            assert results[-1] == {
                "direction": "system",
                "type": "system",
                "text": "Platform notification",
                "datetime": "2026-10-07T22:13:39",
                "attachments": [],
            }
        finally:
            await browser.close()
