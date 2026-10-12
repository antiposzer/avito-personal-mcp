"""Durable local search monitoring. No chat access or external notifications."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from avito_personal_mcp.browser import connect_to_chrome
from avito_personal_mcp.config import Settings
from avito_personal_mcp.listing_date import SARATOV, parse_listing_date
from avito_personal_mcp.listing_detail import discover_listing_detail
from avito_personal_mcp.search import SearchDiscoveryError, search_avito, validate_city
from avito_personal_mcp.seller_type import infer_seller_type

INTERVAL = 300
RESULT_LIMIT = 50
DETAIL_LIMIT = 10


def validate_config(
    query: str,
    city: str | None = None,
    condition: str = "any",
    seller_type: str = "any",
    min_rating: float | None = None,
    max_age_minutes: int | None = None,
) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("query must contain 1–200 characters")
    try:
        city = validate_city(city)
    except SearchDiscoveryError as exc:
        raise ValueError(str(exc)) from exc
    if condition not in {"any", "new", "used"}:
        raise ValueError("condition must be any, new or used")
    if seller_type not in {"any", "private", "shop"}:
        raise ValueError("seller_type must be any, private or shop")
    if min_rating is not None and (
        isinstance(min_rating, bool)
        or not isinstance(min_rating, (int, float))
        or not math.isfinite(min_rating)
        or not 0 <= min_rating <= 5
    ):
        raise ValueError("min_rating must be between 0 and 5")
    if max_age_minutes is not None and (
        type(max_age_minutes) is not int or not 1 <= max_age_minutes <= 1440
    ):
        raise ValueError("max_age_minutes must be between 1 and 1440")
    return dict(
        query=" ".join(query.split()),
        city=city,
        condition=condition,
        seller_type=seller_type,
        min_rating=min_rating,
        max_age_minutes=max_age_minutes,
    )


def publication_time(raw: str | None, observed_at: datetime) -> dict:
    """Resolve supported relative labels; never substitute discovery time."""
    result = parse_listing_date(raw, observed_at)
    label = " ".join((raw or "").split()).lstrip("·• ").casefold()
    match = re.fullmatch(r"(\d+)\s+(секунд[ауы]?|минут[ауы]?|час(?:а|ов)?)\s+назад", label)
    if match:
        amount = int(match[1])
        unit = match[2]
        seconds = amount * (
            1 if unit.startswith("секунд") else 60 if unit.startswith("минут") else 3600
        )
        if seconds <= 366 * 86400:
            stamp = observed_at - timedelta(seconds=seconds)
            result.update(
                date_iso=stamp.isoformat(timespec="seconds"),
                date_precision="second" if unit.startswith("секунд") else "minute",
                date_parse_status="resolved",
            )
    return result


def condition_kind(item: dict) -> str:
    text = " ".join([item.get("condition") or "", *item.get("params", [])]).casefold()
    if re.search(r"б/у|бывш.{0,10}употреблен|как нов(?:ый|ое|ая)", text):
        return "used"
    if re.search(r"(?:^|состояние:\s*)нов(?:ый|ое|ая|ые)\b", text):
        return "new"
    return "unknown"


def assess(item: dict, config: dict, now: datetime, city_slug: str | None) -> tuple[bool, str]:
    """Unknown filtered values fail closed. Dates are Avito's displayed dates."""
    if config["city"]:
        city = config["city"].split(",", 1)[0].casefold()
        location = (item.get("location") or "").casefold()
        in_location = re.search(r"(?<!\w)" + re.escape(city) + r"(?!\w)", location)
        slug = urlparse(item.get("url") or "").path.strip("/").split("/")[0]
        if not in_location and not (city_slug and slug == city_slug):
            return False, "city_unknown_or_different"
    if config["condition"] != "any":
        found = condition_kind(item)
        if found != config["condition"]:
            return False, "condition_unknown_or_different"
    if config["min_rating"] is not None:
        raw = (item.get("rating") or "").replace(",", ".").strip()
        match = re.fullmatch(r"([0-5](?:\.\d+)?)", raw)
        if not match or not 0 <= float(match[1]) <= 5 or float(match[1]) < config["min_rating"]:
            return False, "rating_unknown_or_low"
    if config["seller_type"] != "any":
        inferred = item.get("seller_inference") or infer_seller_type(item.get("description"))
        if inferred["type"] != config["seller_type"]:
            return False, "seller_unknown_or_different"
    if config["max_age_minutes"] is not None:
        date = item.get("publication") or publication_time(item.get("date"), now)
        if date.get("date_precision") not in {"minute", "second"} or not date.get("date_iso"):
            return False, "publication_time_unknown"
        stamp = datetime.fromisoformat(date["date_iso"])
        # A rounded 'minutes ago' label is not precise to the second.
        seconds = (now - stamp).total_seconds()
        if not 0 <= seconds <= config["max_age_minutes"] * 60:
            return False, "publication_time_outside_window"
    return True, "matches"


class WatchStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(
            os.getenv(
                "AVITO_MCP_MONITOR_DB", str(Path.home() / ".avito-personal-mcp" / "monitor.sqlite3")
            )
        )

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS watches(
                    id TEXT PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, last_attempt REAL, last_success REAL,
                    error TEXT, counts TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS seen(
                    watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                    listing_id TEXT NOT NULL, payload TEXT NOT NULL, first_seen REAL NOT NULL,
                    decision TEXT, PRIMARY KEY(watch_id,listing_id));
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT NOT NULL,
                    listing_id TEXT NOT NULL, found_at REAL NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(watch_id,listing_id));
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL NOT NULL);
            """)
            with con:
                yield con
        finally:
            con.close()

    def save(self, config: dict, watch_id: str | None = None) -> dict:
        encoded = json.dumps(config, ensure_ascii=False, sort_keys=True)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if watch_id:
                old = con.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
                if not old:
                    raise ValueError("watch not found")
                if old["lease_until"] > time.time():
                    raise ValueError("watch is being checked; retry after completion")
                if old["config"] != encoded:
                    con.execute("DELETE FROM seen WHERE watch_id=?", (watch_id,))
                    con.execute("DELETE FROM events WHERE watch_id=?", (watch_id,))
                    con.execute(
                        "UPDATE watches SET config=?,initialized=0,next_due=0,error=NULL "
                        "WHERE id=?",
                        (encoded, watch_id),
                    )
            else:
                duplicate = con.execute(
                    "SELECT id FROM watches WHERE config=?", (encoded,)
                ).fetchone()
                watch_id = duplicate[0] if duplicate else uuid.uuid4().hex
                if not duplicate:
                    con.execute(
                        "INSERT INTO watches(id,config,enabled) VALUES(?,?,1)", (watch_id, encoded)
                    )
        return next(w for w in self.list() if w["id"] == watch_id)

    def list(self) -> list[dict]:
        with self.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM watches ORDER BY rowid")]
        for row in rows:
            row["config"] = json.loads(row["config"])
            row["counts"] = json.loads(row["counts"])
            row["interval_seconds"] = INTERVAL
            row["enabled"] = bool(row["enabled"])
        return rows

    def enable(self, watch_id: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        with self.connect() as con:
            if not con.execute(
                "UPDATE watches SET enabled=? WHERE id=?", (enabled, watch_id)
            ).rowcount:
                raise ValueError("watch not found")

    def events(
        self, after_id: int = 0, watch_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        if type(after_id) is not int or after_id < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid event cursor or limit")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM events WHERE id>? AND (? IS NULL OR watch_id=?) ORDER BY id LIMIT ?",
                (after_id, watch_id, watch_id, limit),
            ).fetchall()
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def claim(self, now: float) -> dict | None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cooldown = con.execute("SELECT value FROM meta WHERE key='cooldown'").fetchone()
            if cooldown and cooldown[0] > now:
                return None
            row = con.execute(
                "SELECT * FROM watches WHERE enabled=1 AND next_due<=? "
                "AND lease_until<=? ORDER BY next_due,rowid LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            con.execute(
                "UPDATE watches SET lease_until=?,last_attempt=?,next_due=? WHERE id=?",
                (now + 900, now, now + INTERVAL, row["id"]),
            )
            return dict(row, config=json.loads(row["config"]))

    def finish(self, watch_id: str, error: str | None, counts: dict, now: float) -> None:
        delay = 1800 if error == "rate_limited" else INTERVAL
        with self.connect() as con:
            if error == "rate_limited":
                con.execute("INSERT OR REPLACE INTO meta VALUES('cooldown',?)", (now + delay,))
            con.execute(
                "UPDATE watches SET lease_until=0,next_due=?,error=?,counts=?,"
                "last_success=CASE WHEN ? IS NULL THEN ? ELSE last_success END WHERE id=?",
                (now + delay, error, json.dumps(counts), error, now, watch_id),
            )


class BrowserSource:
    async def __aenter__(self):
        self.settings = Settings.from_env()
        self.session = await connect_to_chrome(self.settings)
        try:
            if not self.session.browser.contexts:
                raise RuntimeError("No Chrome context")
            # Only this owned tab is used or closed. Existing chat/profile tabs remain untouched.
            self.page = await self.session.browser.contexts[0].new_page()
            # Use the observed desktop layout even when Chrome starts minimized.
            await self.page.set_viewport_size({"width": 1280, "height": 900})
        except BaseException:
            await self.session.close()
            raise
        return self

    async def __aexit__(self, *args):
        try:
            await self.page.close()
        finally:
            await self.session.close()

    async def search(self, config: dict) -> dict:
        items = await search_avito(
            self.page,
            self.settings.avito_origin,
            config["query"],
            RESULT_LIMIT,
            None,
            None,
            "date_desc",
            config["city"],
            None,
        )
        # The geo slug comes from the observed, city-verified search URL, not transliteration.
        slug = urlparse(self.page.url).path.strip("/").split("/")[0]
        if not config["city"] or slug in {"", "all", "rossiya"}:
            slug = None
        return {"results": items, "city_slug": slug}

    async def detail(self, item: dict) -> dict:
        await asyncio.sleep(3)
        return await discover_listing_detail(self.page, self.settings.avito_origin, item["url"])


class MonitorManager:
    def __init__(self, store: WatchStore | None = None, source_factory=BrowserSource):
        self.store = store or WatchStore()
        self.source_factory = source_factory
        self.panel = None
        self._notifications = None
        self._bot_panel = None

    @property
    def bot_panel(self):
        if self._bot_panel is None:
            from avito_personal_mcp.bot_panel import BotPanel

            self._bot_panel = BotPanel(self.notifications)
        return self._bot_panel

    @property
    def notifications(self):
        if self._notifications is None:
            from avito_personal_mcp.notifications import NotificationService

            self._notifications = NotificationService(self.store)
        return self._notifications

    async def run(self):
        while True:
            try:
                await self.poll_once()
            except Exception:
                # No input contents or browser data go to logs. Try again next tick.
                pass
            await asyncio.sleep(5)

    async def poll_once(self, now: float | None = None) -> bool:
        clock = time.time() if now is None else now
        watch = self.store.claim(clock)
        if watch is None:
            return False
        wid, config = watch["id"], watch["config"]
        counts = dict(observed=0, new_events=0, rejected=0, pending=0, limited=False)
        error = None
        try:
            async with self.source_factory() as source:
                snapshot = await source.search(config)
                items = snapshot["results"]
                counts.update(observed=len(items), limited=len(items) >= RESULT_LIMIT)
                stamp = time.time() if now is None else clock
                with self.store.connect() as con:
                    for item in items:
                        listing_id = str(item["id"])
                        item = dict(
                            item,
                            publication=publication_time(
                                item.get("date"), datetime.fromtimestamp(stamp, SARATOV)
                            ),
                        )
                        con.execute(
                            "INSERT INTO seen VALUES(?,?,?,?,?) "
                            "ON CONFLICT(watch_id,listing_id) DO UPDATE SET "
                            "payload=excluded.payload",
                            (
                                wid,
                                listing_id,
                                json.dumps(item, ensure_ascii=False),
                                stamp,
                                None if watch["initialized"] else "baseline",
                            ),
                        )
                    if not watch["initialized"]:
                        con.execute("UPDATE watches SET initialized=1 WHERE id=?", (wid,))
                    pending = con.execute(
                        "SELECT * FROM seen WHERE watch_id=? AND decision IS NULL "
                        "ORDER BY first_seen DESC,rowid LIMIT 50",
                        (wid,),
                    ).fetchall()
                details = 0
                for row in pending:
                    item = json.loads(row["payload"])
                    moment = datetime.fromtimestamp(stamp, SARATOV)
                    item["publication"] = item.get("publication") or publication_time(
                        item.get("date"), moment
                    )
                    cheap = dict(config, seller_type="any", max_age_minutes=None)
                    if condition_kind(item) == "unknown":
                        cheap["condition"] = "any"
                    passes, reason = assess(item, cheap, moment, snapshot.get("city_slug"))
                    if not passes:
                        counts["rejected"] += 1
                        with self.store.connect() as con:
                            con.execute(
                                "UPDATE seen SET decision=? WHERE watch_id=? AND listing_id=?",
                                (reason, wid, row["listing_id"]),
                            )
                        continue
                    needs_detail = (
                        config["seller_type"] != "any"
                        or (config["condition"] != "any" and condition_kind(item) == "unknown")
                        or (
                            config["max_age_minutes"] is not None
                            and item["publication"]["date_precision"] not in {"minute", "second"}
                        )
                    )
                    if needs_detail:
                        if details >= DETAIL_LIMIT:
                            continue
                        details += 1
                        detail = await source.detail(item)
                        if detail.get("state") == "inactive":
                            counts["rejected"] += 1
                            with self.store.connect() as con:
                                con.execute(
                                    "UPDATE seen SET decision='inactive' WHERE watch_id=? "
                                    "AND listing_id=?",
                                    (wid, row["listing_id"]),
                                )
                            continue
                        item["description"] = detail.get("description")
                        item["params"] = detail.get("params", [])
                        for param in item["params"]:
                            if param.startswith("Состояние:"):
                                item["condition"] = param.split(":", 1)[1].strip()
                        item["seller_inference"] = infer_seller_type(detail.get("description"))
                        item["publication"] = {
                            k: detail.get(k) for k in ("date", "date_iso", "date_precision")
                        }
                    # Assess age against the current time, including time spent loading details.
                    evaluation_time = datetime.fromtimestamp(
                        time.time() if now is None else clock, SARATOV
                    )
                    matches, reason = assess(
                        item, config, evaluation_time, snapshot.get("city_slug")
                    )
                    item["first_seen_at"] = datetime.fromtimestamp(
                        row["first_seen"], SARATOV
                    ).isoformat()
                    with self.store.connect() as con:
                        if matches:
                            saved = con.execute(
                                "INSERT OR IGNORE INTO events(watch_id,listing_id,"
                                "found_at,payload) VALUES(?,?,?,?)",
                                (
                                    wid,
                                    row["listing_id"],
                                    stamp,
                                    json.dumps(item, ensure_ascii=False),
                                ),
                            )
                            counts["new_events"] += saved.rowcount
                        else:
                            counts["rejected"] += 1
                        con.execute(
                            "UPDATE seen SET decision=? WHERE watch_id=? AND listing_id=?",
                            (reason, wid, row["listing_id"]),
                        )
                with self.store.connect() as con:
                    counts["pending"] = con.execute(
                        "SELECT COUNT(*) FROM seen WHERE watch_id=? AND decision IS NULL", (wid,)
                    ).fetchone()[0]
        except asyncio.CancelledError:
            self.store.finish(wid, "interrupted", counts, time.time())
            raise
        except Exception as exc:
            error = "rate_limited" if "429" in str(exc) else "source_unavailable"
        self.store.finish(wid, error, counts, time.time() if now is None else clock)
        return True

    def close_panel(self):
        if self.panel:
            self.panel.close()
            self.panel = None
