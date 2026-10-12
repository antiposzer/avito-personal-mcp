"""Private Telegram chat panel. Only the paired owner can request searches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime

from avito_personal_mcp.monitor import SARATOV, BrowserSource, assess, validate_config
from avito_personal_mcp.notifications import DeliveryError

MENU = {
    "inline_keyboard": [
        [{"text": "🔎 Быстрый поиск", "callback_data": "panel.quick"}],
        [{"text": "🆕 Последние 5 объявлений", "callback_data": "panel.latest"}],
        [{"text": "📋 Мои поиски", "callback_data": "panel.watches"}],
    ]
}


class BotPanel:
    def __init__(self, notifications, source_factory=BrowserSource):
        self.notifications = notifications
        self.store = notifications.store
        self.source_factory = source_factory
        with self.store.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS bot_panel_state(
                    bot_key TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
                    poll_until REAL NOT NULL DEFAULT 0, awaiting_query INTEGER NOT NULL DEFAULT 0,
                    query TEXT, city TEXT, results TEXT, fetched_at REAL,
                    last_search REAL NOT NULL DEFAULT 0, activated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bot_panel_updates(
                    bot_key TEXT NOT NULL, update_id INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(bot_key,update_id));
            """)

    def config(self):
        with self.store.connect() as con:
            row = dict(con.execute("SELECT * FROM notification_config WHERE id=1").fetchone())
        if row["chat_id"] is None:
            raise DeliveryError("not_paired")
        return row

    def key(self, config):
        key = hashlib.sha256(config["bot_username"].encode()).hexdigest()[:24]
        with self.store.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO bot_panel_state(bot_key,activated_at) VALUES(?,?)",
                (key, time.time()),
            )
        return key

    async def call(self, method, payload):
        def invoke():
            with self.notifications.lock:
                api = self.notifications.transport(self.notifications.secrets.load())
                return api.call(method, payload)

        return await asyncio.to_thread(invoke)

    async def send(self, text, markup=MENU):
        config = self.config()
        self.key(config)
        return await self.call(
            "sendMessage",
            {
                "chat_id": config["chat_id"],
                "text": text,
                "link_preview_options": {"is_disabled": True},
                "reply_markup": markup,
            },
        )

    async def open(self):
        config = self.config()
        await self.call(
            "setMyCommands",
            {
                "scope": {"type": "chat", "chat_id": config["chat_id"]},
                "commands": [
                    {"command": "menu", "description": "Панель Avito"},
                    {"command": "search", "description": "Быстрый поиск: /search запрос"},
                    {"command": "latest", "description": "Последние 5 объявлений"},
                    {"command": "watches", "description": "Мои сохранённые поиски"},
                ],
            },
        )
        await self.call(
            "setChatMenuButton", {"chat_id": config["chat_id"], "menu_button": {"type": "commands"}}
        )
        await self.send(
            "Панель Avito\nВыбери быстрый поиск или последние результаты.\n"
            "Для поиска можно сразу отправить: /search iPhone 15 Pro Max"
        )
        return {"status": "ok", "panel_sent": True}

    async def run(self):
        while True:
            try:
                await self.poll_once()
            except Exception:
                pass  # Never echo Telegram URLs, tokens or incoming messages.
            await asyncio.sleep(2)

    async def poll_once(self):
        try:
            config = self.config()
        except DeliveryError:
            return False
        key, now = self.key(config), time.time()
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            state = con.execute("SELECT * FROM bot_panel_state WHERE bot_key=?", (key,)).fetchone()
            if state["poll_until"] > now:
                return False
            con.execute("UPDATE bot_panel_state SET poll_until=? WHERE bot_key=?", (now + 300, key))
            offset = state["offset"]
        hold = 0
        try:
            updates = await self.call("getUpdates", {"offset": offset, "limit": 100, "timeout": 10})
            for update in updates:
                identifier = update.get("update_id")
                if type(identifier) is not int:
                    continue
                with self.store.connect() as con:
                    claimed = con.execute(
                        "INSERT OR IGNORE INTO bot_panel_updates VALUES(?,?,?,?)",
                        (key, identifier, "processing", time.time()),
                    ).rowcount
                status = "ignored"
                if claimed:
                    try:
                        status = await self.handle(update, config, key)
                    except DeliveryError as exc:
                        status = exc.code  # Ambiguous sends are not automatically replayed.
                    except Exception:
                        status = "failed"
                with self.store.connect() as con:
                    if claimed:
                        con.execute(
                            "UPDATE bot_panel_updates SET status=? WHERE bot_key=? AND update_id=?",
                            (status, key, identifier),
                        )
                    con.execute(
                        "UPDATE bot_panel_state SET offset=MAX(offset,?) WHERE bot_key=?",
                        (identifier + 1, key),
                    )
            return True
        except DeliveryError as exc:
            hold = max(5, exc.retry_after) if exc.code == "http_429" else 5
            raise
        finally:
            with self.store.connect() as con:
                con.execute(
                    "UPDATE bot_panel_state SET poll_until=? WHERE bot_key=?",
                    (time.time() + hold if hold else 0, key),
                )

    async def handle(self, update, config, key):
        callback = update.get("callback_query")
        message = (callback or {}).get("message") if callback else update.get("message")
        if not isinstance(message, dict):
            return "ignored"
        sender = (callback or message).get("from") or {}
        chat = message.get("chat") or {}
        if (
            chat.get("type") != "private"
            or chat.get("id") != config["chat_id"]
            or sender.get("id") != config["chat_id"]
            or sender.get("is_bot") is True
        ):
            return "unauthorized"
        if not callback:
            with self.store.connect() as con:
                activated = con.execute(
                    "SELECT activated_at FROM bot_panel_state WHERE bot_key=?", (key,)
                ).fetchone()[0]
            if message.get("date", 0) < activated - 1:
                return "ignored_old_message"
        if callback:
            try:
                await self.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            except DeliveryError as exc:
                if exc.code != "http_400":
                    raise
            action = callback.get("data", "")
            command, query = {
                "panel.quick": ("/search", ""),
                "panel.latest": ("/latest", ""),
                "panel.watches": ("/watches", ""),
            }.get(action, ("", ""))
        else:
            text = (message.get("text") or "").strip()
            if not text:
                return "ignored"
            pieces = text.split(maxsplit=1)
            command = pieces[0].split("@", 1)[0].casefold()
            query = pieces[1] if len(pieces) > 1 else ""
            if not text.startswith("/"):
                with self.store.connect() as con:
                    waiting = con.execute(
                        "SELECT awaiting_query FROM bot_panel_state WHERE bot_key=?", (key,)
                    ).fetchone()[0]
                command, query = ("/search", text) if waiting else ("/menu", "")
        if command in {"/start", "/menu"}:
            if command == "/start" and query:
                return "ignored"  # Pairing commands belong to the existing explicit pairing flow.
            await self.send(
                "Панель Avito\nНажми «Быстрый поиск» или отправь /search и свой запрос."
            )
        elif command == "/search":
            if query:
                await self.quick_search(query, key)
            else:
                with self.store.connect() as con:
                    con.execute(
                        "UPDATE bot_panel_state SET awaiting_query=1 WHERE bot_key=?", (key,)
                    )
                await self.send(
                    "Напиши запрос, например: iPhone 15 Pro Max.\n"
                    f"Город: {self.city() or 'все города'}. Верну до 5 объявлений по дате."
                )
        elif command == "/latest":
            await self.latest(key)
        elif command == "/watches":
            watches = self.store.list()
            lines = ["Мои сохранённые поиски"]
            for watch in watches[:10]:
                c = watch["config"]
                lines.append(
                    f"• {c['query']} — {c['city'] or 'все города'}; "
                    f"{'работает' if watch['enabled'] else 'на паузе'}"
                )
            await self.send("\n".join(lines) if watches else "Сохранённых поисков пока нет.")
        else:
            return "ignored"
        return "done"

    def city(self):
        watches = self.store.list()
        return next((w["config"]["city"] for w in reversed(watches) if w["config"]["city"]), None)

    async def quick_search(self, query, key):
        try:
            config = validate_config(query, city=self.city())
        except ValueError:
            await self.send("Запрос должен содержать от 1 до 200 символов.")
            return
        now = time.time()
        blocked = None
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cooldown = con.execute("SELECT value FROM meta WHERE key='cooldown'").fetchone()
            last = con.execute(
                "SELECT last_search FROM bot_panel_state WHERE bot_key=?", (key,)
            ).fetchone()[0]
            if cooldown and cooldown[0] > now:
                wait = max(1, round((cooldown[0] - now) / 60))
                blocked = (
                    f"Авито ограничил запросы. Повторный поиск примерно через {wait} мин.\n"
                    + ("Кнопка «Последние 5» покажет уже найденные результаты.")
                )
            elif now - last < 30:
                blocked = "Подожди немного между поисками. Последние результаты доступны по кнопке."
            else:
                con.execute(
                    "UPDATE bot_panel_state SET last_search=?,awaiting_query=0 WHERE bot_key=?",
                    (now, key),
                )
        if blocked:
            await self.send(blocked)
            return
        await self.send("Ищу последние объявления…")
        try:
            async with self.source_factory() as source:
                snapshot = await source.search(config)
            moment = datetime.now(SARATOV)
            found = [
                item
                for item in snapshot["results"]
                if assess(item, config, moment, snapshot.get("city_slug"))[0]
            ][:5]
        except Exception as exc:
            if "429" in str(exc):
                with self.store.connect() as con:
                    con.execute(
                        "INSERT OR REPLACE INTO meta VALUES('cooldown',?)", (time.time() + 1800,)
                    )
                await self.send(
                    "Авито ограничил запросы. Сохранённые результаты остаются доступны."
                )
            else:
                await self.send("Поиск сейчас недоступен. Проверь, что MCP и Chrome запущены.")
            return
        with self.store.connect() as con:
            con.execute(
                "UPDATE bot_panel_state SET query=?,city=?,results=?,fetched_at=? WHERE bot_key=?",
                (
                    config["query"],
                    config["city"],
                    json.dumps(found, ensure_ascii=False),
                    time.time(),
                    key,
                ),
            )
        await self.show_results(found, config["query"], config["city"])

    async def latest(self, key):
        with self.store.connect() as con:
            state = con.execute("SELECT * FROM bot_panel_state WHERE bot_key=?", (key,)).fetchone()
        if state["results"] is not None:
            stamp = datetime.fromtimestamp(state["fetched_at"], SARATOV).strftime("%d.%m %H:%M")
            await self.show_results(
                json.loads(state["results"]), state["query"], state["city"], stamp
            )
            return
        with self.store.connect() as con:
            events = [
                json.loads(row[0])
                for row in con.execute("SELECT payload FROM events ORDER BY id DESC LIMIT 5")
            ]
        if events:
            await self.show_results(events, "Найдено мониторингом", None)
        else:
            await self.send("Пока нет результатов. Нажми «Быстрый поиск» и напиши запрос.")

    async def show_results(self, items, query, city, stamp=None):
        header = f"🔎 {query}\nГород: {city or 'все города'}\n"
        header += (
            f"Сохранённый поиск от {stamp}.\n" if stamp else "Выдача Avito с сортировкой по дате.\n"
        )
        if not items:
            await self.send(header + "Подходящих карточек в проверенной выдаче не найдено.")
            return
        blocks = []
        for number, item in enumerate(items[:5], 1):
            blocks.append(
                f"{number}. {(item.get('title') or 'Объявление')[:140]}\n"
                f"{item.get('price') or 'Цена не указана'}\n"
                f"{(item.get('location') or 'Место не указано')[:100]}\n"
                f"Дата Авито: {(item.get('date') or 'неизвестна')[:60]}\n{item['url']}"
            )
        text = header
        for block in blocks:
            if len(text) + len(block) + 2 > 3900:
                await self.send(text)
                await asyncio.sleep(1.1)
                text = ""
            text += "\n" + block + "\n"
        await self.send(text)
