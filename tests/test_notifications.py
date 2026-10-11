import json
import os
import time
from pathlib import Path

import pytest

from avito_personal_mcp.monitor import WatchStore
from avito_personal_mcp.notifications import DeliveryError, NotificationService, SecretStore


class MemorySecret:
    def save(self, token):
        self.token = token

    def load(self):
        return self.token


class FakeTelegram:
    sends = []
    updates = []
    failure = None
    webhook = False

    def __init__(self, token):
        self.token = token

    def call(self, method, payload):
        if method == "getMe":
            return {"username": "ExampleTestBot"}
        if method == "getWebhookInfo":
            return {"url": "https://example.invalid" if self.webhook else ""}
        if method == "getUpdates":
            return self.updates
        if method == "sendMessage":
            if self.failure:
                raise self.failure
            self.sends.append(payload)
            return {"message_id": len(self.sends)}
        raise AssertionError(method)


@pytest.fixture
def service(tmp_path):
    FakeTelegram.sends, FakeTelegram.updates = [], []
    FakeTelegram.failure, FakeTelegram.webhook = None, False
    return NotificationService(WatchStore(tmp_path / "state.db"), FakeTelegram, MemorySecret())


def connect(service):
    service.setup("test-token-not-real")
    FakeTelegram.updates = [
        {
            "message": {
                "text": "/start " + service.pairing["nonce"],
                "date": int(time.time()),
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
            }
        }
    ]
    return service.pair()


def event(service, number):
    with service.store.connect() as con:
        con.execute(
            "INSERT INTO events(watch_id,listing_id,found_at,payload) VALUES(?,?,?,?)",
            (
                "example-watch",
                str(number),
                time.time(),
                json.dumps(
                    {
                        "title": "Example product",
                        "url": f"https://www.avito.ru/saratov/tovary/x_{number}",
                        "price": "1000 ₽",
                        "rating": "4.8",
                        "location": "Саратов",
                    }
                ),
            ),
        )


def test_pairing_requires_nonce_private_chat_and_matching_sender(service):
    assert service.setup("test-token-not-real")["pair_url"].startswith("https://t.me/")
    for chat_type, nonce, sender in [
        ("group", service.pairing["nonce"], 123),
        ("private", "other", 123),
        ("private", service.pairing["nonce"], 456),
    ]:
        FakeTelegram.updates = [
            {
                "message": {
                    "text": "/start " + nonce,
                    "date": int(time.time()),
                    "chat": {"id": 123, "type": chat_type},
                    "from": {"id": sender},
                }
            }
        ]
        with pytest.raises(DeliveryError, match="start_not_received"):
            service.pair()
    assert FakeTelegram.sends == []
    assert "token" not in json.dumps(service.status())


def test_only_new_events_send_once_including_restart_and_manual_test(service):
    event(service, 1)
    assert connect(service)["enabled"]
    assert not service.deliver_once()  # Existing events are not sent on activation.
    event(service, 2)
    assert service.deliver_once()
    restarted = NotificationService(service.store, FakeTelegram, service.secrets)
    assert not restarted.deliver_once()
    assert len(FakeTelegram.sends) == 1
    assert FakeTelegram.sends[0]["chat_id"] == 123
    assert "https://www.avito.ru/" in FakeTelegram.sends[0]["text"]
    assert service.test()["sent"]


def test_rate_limit_waits_before_any_next_event(service):
    connect(service)
    event(service, 1)
    event(service, 2)
    FakeTelegram.failure = DeliveryError("http_429", 120)
    assert service.deliver_once()
    FakeTelegram.failure = None
    assert not service.deliver_once()
    with service.store.connect() as con:
        con.execute("UPDATE notification_config SET retry_until=0")
        con.execute("UPDATE deliveries SET retry_at=0")
    assert service.deliver_once()
    assert len(FakeTelegram.sends) == 1


def test_uncertain_delivery_is_not_automatically_resent(service):
    connect(service)
    event(service, 1)
    FakeTelegram.failure = DeliveryError("delivery_unconfirmed")
    assert service.deliver_once()
    FakeTelegram.failure = None
    assert not service.deliver_once()
    assert service.status()["counts"]["uncertain"] == 1


def test_explicit_rejection_pauses_delivery_and_webhook_is_not_deleted(service):
    connect(service)
    event(service, 1)
    FakeTelegram.failure = DeliveryError("http_403")
    assert service.deliver_once()
    assert not service.status()["enabled"]
    FakeTelegram.webhook = True
    with pytest.raises(DeliveryError, match="bot_has_webhook"):
        service.setup("test-token-not-real")


def test_secret_round_trip_and_windows_file_does_not_contain_plain_token(tmp_path: Path):
    secret = SecretStore(tmp_path / "credential.secret")
    value = "fake-test-credential"
    secret.save(value)
    assert secret.load() == value
    if os.name == "nt":
        assert value.encode() not in secret.path.read_bytes()
    else:
        assert secret.path.stat().st_mode & 0o777 == 0o600


def test_pair_command_can_be_restored_without_token_and_accepts_command_suffix(service):
    service.setup("test-token-not-real")
    old = service.pairing["nonce"]
    restored = service.resume_pair()
    assert restored["pair_command"] != "/start " + old
    assert service.status()["pair_command"] == restored["pair_command"]
    FakeTelegram.updates = [
        {
            "message": {
                "text": " \n/start@ExampleTestBot " + service.pairing["nonce"] + " \n",
                "date": int(time.time()),
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
            }
        }
    ]
    assert service.pair()["paired"]
    with pytest.raises(DeliveryError, match="already_paired"):
        service.resume_pair()
