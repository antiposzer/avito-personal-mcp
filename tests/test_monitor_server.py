import pytest

from avito_personal_mcp import server
from avito_personal_mcp.cli import build_parser, command_to_tool
from avito_personal_mcp.monitor import MonitorManager, WatchStore


@pytest.mark.asyncio
async def test_monitor_tools_save_pause_and_preserve_empty_feed_cursor(tmp_path, monkeypatch):
    manager = MonitorManager(WatchStore(tmp_path / "server.db"))
    monkeypatch.setattr(server, "_monitor", manager)
    saved = await server.avito_monitor_save("Any product", city="Саратов", min_rating=4.8)
    assert saved["status"] == "ok"
    wid = saved["watch"]["id"]
    assert len((await server.avito_monitor_list())["watches"]) == 1
    assert (await server.avito_monitor_enable(wid, False))["status"] == "ok"
    assert not manager.store.list()[0]["enabled"]
    assert (await server.avito_monitor_events(42))["next_cursor"] == 42
    assert (await server.avito_monitor_save("Product", min_rating=8))["status"] == "invalid_request"


def test_monitor_cli_commands_map_only_their_own_arguments():
    parser = build_parser()
    assert command_to_tool(parser.parse_args(["monitor-list"])) == ("avito_monitor_list", {})
    assert command_to_tool(parser.parse_args(["monitor-events", "--after-id", "42"])) == (
        "avito_monitor_events",
        {"after_id": 42, "limit": 100},
    )
