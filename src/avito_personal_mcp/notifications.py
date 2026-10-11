"""Opt-in Telegram delivery with local protected credentials and a durable outbox."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeliveryError(Exception):
    def __init__(self, code: str, retry_after: int = 60):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)  # Never expose Telegram request URLs or bot tokens.


class SecretStore:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def crypt(data: bytes, decrypt=False) -> bytes:
        class Blob(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        api = ctypes.windll.crypt32
        function = api.CryptUnprotectData if decrypt else api.CryptProtectData
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise DeliveryError("credential_storage_unavailable")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            ctypes.windll.kernel32.LocalFree(target.data)

    def save(self, token: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = token.encode()
        protected = (
            b"dpapi:" + base64.b64encode(self.crypt(raw)) if os.name == "nt" else b"local:" + raw
        )
        with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as output:
            temp = Path(output.name)
            output.write(protected)
        try:
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self) -> str:
        raw = self.path.read_bytes()
        if raw.startswith(b"dpapi:"):
            return self.crypt(base64.b64decode(raw[6:]), decrypt=True).decode()
        if raw.startswith(b"local:") and os.name != "nt":
            return raw[6:].decode()
        raise DeliveryError("credential_storage_unavailable")


class Telegram:
    def __init__(self, token: str):
        if not re.fullmatch(r"\d{5,16}:[A-Za-z0-9_-]{25,100}", token):
            raise DeliveryError("invalid_token")
        self.token = token

    def call(self, method: str, payload: dict):
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                result = json.load(response)
        except HTTPError as exc:
            delay = 60
            if exc.code == 429:
                try:
                    delay = int(json.load(exc).get("parameters", {}).get("retry_after", 60))
                except (ValueError, KeyError):
                    pass
            raise DeliveryError(f"http_{exc.code}", delay) from None
        except (URLError, OSError, ValueError):
            # Telegram has no sendMessage idempotency key. A lost reply is ambiguous.
            code = "delivery_unconfirmed" if method == "sendMessage" else "telegram_unavailable"
            raise DeliveryError(code) from None
        if not result.get("ok"):
            raise DeliveryError("telegram_rejected")
        return result["result"]


def message_text(event: dict) -> str:
    item = event["payload"]
    inference = item.get("seller_inference") or {}
    date = (item.get("publication") or {}).get("date") or item.get("date") or "неизвестно"
    lines = [
        "Новое объявление",
        item.get("title") or "Без названия",
        "Цена: " + (item.get("price") or "не указана"),
        "Место: " + (item.get("location") or "неизвестно"),
        "Состояние: " + (item.get("condition") or "неизвестно"),
        "Продавец: " + inference.get("label", "не определено") + " (по описанию)",
        "Рейтинг: " + (item.get("rating") or "неизвестен"),
        "Дата Авито: " + date,
    ]
    # Keep the exact observed link and event identifier intact when truncating titles.
    return "\n".join(lines)[:3300] + "\n" + item["url"] + f"\nСобытие №{event['id']}"


class NotificationService:
    def __init__(self, store, transport=Telegram, secret_store=None):
        self.store, self.transport = store, transport
        self.secrets = secret_store or SecretStore(store.path.with_name("telegram-token.secret"))
        self.pairing = None
        self.lock = threading.Lock()
        self.initialize()

    def initialize(self):
        with self.store.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS notification_config(
                  id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                  bot_username TEXT, chat_id INTEGER, start_after INTEGER NOT NULL DEFAULT 0,
                  retry_until REAL NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO notification_config(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS deliveries(
                  event_id INTEGER PRIMARY KEY, status TEXT NOT NULL, claimed_at REAL,
                  retry_at REAL NOT NULL DEFAULT 0, error TEXT, message_id INTEGER);
            """)

    def status(self):
        with self.store.connect() as con:
            config = dict(con.execute("SELECT * FROM notification_config WHERE id=1").fetchone())
            last_test = con.execute(
                "SELECT value FROM meta WHERE key='telegram_last_test'"
            ).fetchone()
            counts = dict(
                con.execute(
                    "SELECT d.status,COUNT(*) FROM deliveries d JOIN events e "
                    "ON e.id=d.event_id WHERE e.id>? GROUP BY d.status",
                    (config["start_after"],),
                )
            )
        # Recipient and credentials never appear in API responses or logs.
        result = dict(
            enabled=bool(config["enabled"]),
            paired=config["chat_id"] is not None,
            bot_username=config["bot_username"],
            counts=counts,
            last_test_at=last_test[0] if last_test else None,
        )
        if self.pairing and self.pairing["expires"] > time.time():
            nonce = self.pairing["nonce"]
            result.update(pair_url=f"https://t.me/{config['bot_username']}?start={nonce}",
                          pair_command="/start " + nonce)
        return result

    def setup(self, token: str):
        with self.lock:
            api = self.transport(token)
            identity = api.call("getMe", {})
            webhook = api.call("getWebhookInfo", {})
            if webhook.get("url"):
                raise DeliveryError("bot_has_webhook")
            username = identity.get("username", "")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", username):
                raise DeliveryError("invalid_bot")
            nonce = secrets.token_urlsafe(24)
            self.pairing = dict(nonce=nonce, expires=time.time() + 600, started=time.time())
            with self.store.connect() as con:
                con.execute(
                    "UPDATE notification_config SET enabled=0,chat_id=NULL,bot_username=? "
                    "WHERE id=1",
                    (username,),
                )
            self.secrets.save(token)
            return {"pair_url": f"https://t.me/{username}?start={nonce}",
                    "pair_command": "/start " + nonce, "expires_in_seconds": 600}

    def resume_pair(self):
        if self.status()["paired"]:
            raise DeliveryError("already_paired")
        try:
            token = self.secrets.load()
        except (OSError, ValueError):
            raise DeliveryError("not_paired") from None
        return self.setup(token)

    def pair(self):
        with self.lock:
            if not self.pairing or self.pairing["expires"] < time.time():
                raise DeliveryError("pairing_expired")
            api = self.transport(self.secrets.load())
            updates = api.call("getUpdates", {"timeout": 0, "limit": 100})
            username = self.status()["bot_username"].casefold()
            chat_id = None
            for update in updates:
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                parts = (msg.get("text") or "").strip().split()
                valid_command = len(parts) == 2 and parts[1] == self.pairing["nonce"] and (
                    parts[0].casefold() in {"/start", "/start@" + username})
                if (
                    chat.get("type") == "private"
                    and valid_command
                    and msg.get("date", 0) >= self.pairing["started"] - 30
                    and chat.get("id") == (msg.get("from") or {}).get("id")
                ):
                    chat_id = chat["id"]
            if chat_id is None:
                raise DeliveryError("start_not_received")
            with self.store.connect() as con:
                latest = con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
                con.execute(
                    "UPDATE notification_config SET chat_id=?,enabled=1,start_after=? WHERE id=1",
                    (chat_id, latest),
                )
            self.pairing = None
            return self.status()

    def enable(self, enabled: bool):
        if type(enabled) is not bool:
            raise DeliveryError("invalid_request")
        with self.store.connect() as con:
            config = con.execute("SELECT * FROM notification_config WHERE id=1").fetchone()
            if enabled and config["chat_id"] is None:
                raise DeliveryError("not_paired")
            con.execute("UPDATE notification_config SET enabled=? WHERE id=1", (enabled,))
        return self.status()

    def test(self):
        with self.lock, self.store.connect() as con:
            config = con.execute("SELECT * FROM notification_config WHERE id=1").fetchone()
            if config["chat_id"] is None:
                raise DeliveryError("not_paired")
            self.transport(self.secrets.load()).call(
                "sendMessage",
                {
                    "chat_id": config["chat_id"],
                    "text": "Уведомления Avito подключены. "
                    "Новые подходящие объявления будут приходить сюда.",
                },
            )
            con.execute(
                "INSERT OR REPLACE INTO meta VALUES('telegram_last_test',?)", (time.time(),)
            )
        return {"sent": True}

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.deliver_once)
            except Exception:
                pass
            await asyncio.sleep(5)

    def deliver_once(self):
        # Configuration changes and sends use the same lock. No send can use an unpaired recipient.
        with self.lock:
            now = time.time()
            with self.store.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                config = dict(
                    con.execute("SELECT * FROM notification_config WHERE id=1").fetchone()
                )
                if not config["enabled"] or config["chat_id"] is None:
                    return False
                if config["retry_until"] > now:
                    return False
                con.execute(
                    "UPDATE deliveries SET status='uncertain',error='delivery_unconfirmed' "
                    "WHERE status='sending' AND claimed_at<?",
                    (now - 120,),
                )
                con.execute(
                    "INSERT OR IGNORE INTO deliveries(event_id,status) "
                    "SELECT id,'pending' FROM events WHERE id>?",
                    (config["start_after"],),
                )
                row = con.execute(
                    "SELECT e.*,d.status FROM events e JOIN deliveries d ON d.event_id=e.id "
                    "WHERE d.status IN ('pending','retry') AND d.retry_at<=? "
                    "ORDER BY e.id LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    return False
                event = dict(row, payload=json.loads(row["payload"]))
                con.execute(
                    "UPDATE deliveries SET status='sending',claimed_at=? WHERE event_id=?",
                    (now, event["id"]),
                )
                try:
                    token = self.secrets.load()
                except (DeliveryError, OSError, ValueError):
                    con.execute(
                        "UPDATE deliveries SET status='failed',"
                        "error='credential_storage_unavailable' "
                        "WHERE event_id=?",
                        (event["id"],),
                    )
                    con.execute("UPDATE notification_config SET enabled=0 WHERE id=1")
                    return True
            try:
                result = self.transport(token).call(
                    "sendMessage",
                    {
                        "chat_id": config["chat_id"],
                        "text": message_text(event),
                        "link_preview_options": {"is_disabled": True},
                    },
                )
                status, error, retry_at, mid = "sent", None, 0, result["message_id"]
            except DeliveryError as exc:
                status = (
                    "uncertain"
                    if exc.code in {"delivery_unconfirmed", "http_500", "http_502", "http_503"}
                    else ("retry" if exc.code == "http_429" else "failed")
                )
                error, retry_at, mid = exc.code, now + max(5, exc.retry_after), None
            except (OSError, ValueError):
                status, error, retry_at, mid = "failed", "credential_storage_unavailable", 0, None
            with self.store.connect() as con:
                con.execute(
                    "UPDATE deliveries SET status=?,error=?,retry_at=?,message_id=? "
                    "WHERE event_id=?",
                    (status, error, retry_at, mid, event["id"]),
                )
                if status == "failed":
                    con.execute("UPDATE notification_config SET enabled=0 WHERE id=1")
                if status == "retry":
                    con.execute(
                        "UPDATE notification_config SET retry_until=? WHERE id=1", (retry_at,)
                    )
            return True
