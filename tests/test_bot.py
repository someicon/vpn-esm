from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from vless_reality.bot import drain_outbox, encode_photo, handle_update, qr_png
from vless_reality.daemon import Daemon, runtime_from_heartbeat
from vless_reality.service import Service


class FakeTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, bytes, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    def send_photo(self, chat_id: int, png: bytes, caption: str) -> None:
        self.photos.append((chat_id, png, caption))

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        return []

    def set_commands(self, commands: list[dict]) -> None:
        self.commands = commands


class _Dead:
    returncode = 1


class FakeXray:
    def __init__(self) -> None:
        self.starts = 0
        self.alive = False
        self.proc = None
        self.started_at = 0.0

    def running(self) -> bool:
        return self.alive

    def pid(self) -> int | None:
        return 4321 if self.alive else None

    def start(self) -> None:
        self.starts += 1
        self.alive = True
        self.proc = _Dead()
        self.started_at = time.monotonic()

    def stop(self) -> None:
        self.alive = False
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()


def _message(text: str, user_id: int, chat_type: str = "private") -> dict:
    return {
        "update_id": 1,
        "message": {
            "text": text,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id},
        },
    }


def test_stranger_is_told_their_id(service: Service) -> None:
    transport = FakeTransport()
    handle_update(_message("/status", 7), service, transport, "Xray: остановлен")
    assert transport.photos == []
    assert "Нет доступа" in transport.messages[0][1]
    assert "7" in transport.messages[0][1]


def test_admin_add_sends_qr(service: Service) -> None:
    transport = FakeTransport()
    handle_update(_message("/add alice", 42), service, transport, "Xray: работает, pid 1")
    assert service.store.user("alice") is not None
    _chat, png, caption = transport.photos[0]
    assert png.startswith(b"\x89PNG")
    assert "vless://alice" not in caption
    assert "vless://" in caption
    assert "alice" in caption


def test_group_chat_is_ignored(service: Service) -> None:
    transport = FakeTransport()
    handle_update(_message("/add alice", 42, chat_type="group"), service, transport, "")
    assert transport.messages == []
    assert transport.photos == []
    assert service.store.user("alice") is None


def test_cli_add_reaches_the_admin_outbox(service: Service) -> None:
    _user, link = service.add_user("bob", notify=True)
    transport = FakeTransport()
    drain_outbox(service, transport)
    assert link in transport.messages[0][1]
    assert service.store.outbox() == []


def test_qr_and_photo_body() -> None:
    png = qr_png("vless://example")
    content_type, body = encode_photo(42, png, "caption")
    assert content_type.startswith("multipart/form-data")
    assert b'name="chat_id"' in body
    assert b"\x89PNG" in body
    assert b"caption" in body


def test_daemon_applies_config_and_backs_off(service: Service, tmp_path: Path) -> None:
    daemon = Daemon(service, "xray", tmp_path)
    fake = FakeXray()
    daemon.xray = fake
    daemon.tick()
    assert fake.starts == 1
    document = json.loads((tmp_path / "xray.json").read_text(encoding="utf-8"))
    assert document["inbounds"][0]["streamSettings"]["realitySettings"]["target"] == "www.example.com:443"
    assert "Демон: работает" in runtime_from_heartbeat(tmp_path / "daemon.json")

    service.add_user("anna")
    daemon.tick()
    assert fake.starts == 2
    clients = json.loads((tmp_path / "xray.json").read_text(encoding="utf-8"))
    assert clients["inbounds"][0]["settings"]["clients"][0]["email"] == "anna"

    fake.alive = False
    fake.proc = _Dead()
    fake.started_at = time.monotonic()
    daemon.tick()
    assert fake.starts == 2
    assert daemon.next_start > time.monotonic()
