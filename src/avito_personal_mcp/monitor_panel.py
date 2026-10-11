"""Loopback-only monitoring panel; contains public listing data, never chats."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from avito_personal_mcp.monitor import validate_config
from avito_personal_mcp.notifications import DeliveryError, NotificationService

HTML = files("avito_personal_mcp").joinpath("static/monitor.html").read_text(encoding="utf-8")


class MonitorPanel:
    def __init__(self, store, notifications=None):
        self.store = store
        self.notifications = notifications or NotificationService(store)
        self.token = secrets.token_urlsafe(32)
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body, content_type="application/json; charset=utf-8"):
                data = (
                    body.encode()
                    if isinstance(body, str)
                    else json.dumps(body, ensure_ascii=False).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; "
                    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'none'",
                )
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    # Closing/reloading the panel can abort an in-flight refresh.
                    # Do not log request bodies or try to send a second response.
                    return

            def allowed(self):
                return self.headers.get("Host") == panel.address

            def do_GET(self):
                if not self.allowed():
                    return self.send(403, {"error": "Доступ запрещён"})
                if self.path == "/":
                    return self.send(
                        200, HTML.replace("__TOKEN__", panel.token), "text/html; charset=utf-8"
                    )
                if self.path.startswith("/api/state?"):
                    try:
                        from urllib.parse import parse_qs, urlparse

                        cursor = int(parse_qs(urlparse(self.path).query).get("after_id", ["0"])[0])
                        return self.send(
                            200,
                            {
                                "watches": panel.store.list(),
                                "events": panel.store.events(cursor),
                                "notifications": panel.notifications.status(),
                            },
                        )
                    except (ValueError, OSError, sqlite3.Error):
                        return self.send(400, {"error": "Не удалось прочитать мониторинг"})
                return self.send(404, {"error": "Страница не найдена"})

            def do_POST(self):
                if (
                    not self.allowed()
                    or self.headers.get("Origin") != panel.url.rstrip("/")
                    or (self.headers.get("X-Monitor-Token") != panel.token)
                ):
                    return self.send(403, {"error": "Доступ запрещён"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("invalid body size")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("invalid body")
                    if self.path == "/api/save":
                        watch_id = data.pop("watch_id", None)
                        result = panel.store.save(validate_config(**data), watch_id)
                    elif self.path == "/api/enable":
                        panel.store.enable(data["watch_id"], data["enabled"])
                        result = {"status": "ok"}
                    elif self.path == "/api/telegram/setup":
                        result = panel.notifications.setup(data["token"])
                    elif self.path == "/api/telegram/pair":
                        result = panel.notifications.pair()
                    elif self.path == "/api/telegram/resume":
                        result = panel.notifications.resume_pair()
                    elif self.path == "/api/telegram/enable":
                        result = panel.notifications.enable(data["enabled"])
                    elif self.path == "/api/telegram/test":
                        result = panel.notifications.test()
                    else:
                        return self.send(404, {"error": "Действие не найдено"})
                    return self.send(200, result)
                except DeliveryError as exc:
                    labels = {
                        "invalid_token": "Проверьте токен бота.",
                        "http_401": "Telegram не принял токен. Проверьте его в BotFather.",
                        "http_403": "Бот заблокирован. Разблокируйте его в Telegram.",
                        "http_409": "Бот используется другим подключением. "
                        "Используйте отдельного бота.",
                        "bot_has_webhook": "У бота уже настроен другой сервис. "
                        "Используйте отдельного бота.",
                        "pairing_expired": "Ссылка подключения устарела. Сохраните токен снова.",
                        "start_not_received": "Откройте ссылку на бота и нажмите Старт, "
                        "затем подтвердите ещё раз.",
                        "not_paired": "Сначала подключите личный чат с ботом.",
                        "telegram_unavailable": "Не удалось подключиться к Telegram. "
                                                "Проверьте соединение и попробуйте позже.",
                        "delivery_unconfirmed": "Telegram не подтвердил отправку. "
                        "Повтор может создать дубль.",
                    }
                    return self.send(
                        400,
                        {
                            "error": labels.get(
                                exc.code, "Telegram сейчас недоступен. Попробуйте позже."
                            )
                        },
                    )
                except (ValueError, TypeError, KeyError):
                    return self.send(
                        400,
                        {
                            "error": "Проверьте поля: запрос, город, состояние, "
                            "тип продавца, рейтинг 0–5 и время 1–1440 минут."
                        },
                    )
                except (OSError, sqlite3.Error):
                    return self.send(500, {"error": "Не удалось сохранить мониторинг"})

        port = int(os.getenv("AVITO_MCP_MONITOR_PORT", "0"))
        if port != 0 and not 1024 <= port <= 65535:
            raise ValueError("invalid local monitor port")
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.address = f"127.0.0.1:{self.server.server_port}"
        self.url = f"http://{self.address}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
