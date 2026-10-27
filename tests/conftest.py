from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from vless_reality.service import Service
from vless_reality.store import Store


def _resolve(_host: str):
    return [ipaddress.ip_address("203.0.113.10")]


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Service:
    monkeypatch.setenv("VR_HOME", str(tmp_path))
    svc = Service(Store(tmp_path / "state.db"))
    svc.setup(
        address="203.0.113.10",
        dest="www.example.com:443",
        bot_token="1:test-token",
        admin_ids=[42],
        resolve=_resolve,
    )
    return svc
