from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from vless_reality.cli import main
from vless_reality.store import Store


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VR_HOME", str(tmp_path))
    monkeypatch.setattr(
        "vless_reality.service.resolve_ips",
        lambda _host: [ipaddress.ip_address("203.0.113.10")],
    )
    return tmp_path


def test_setup_user_and_redacted_config(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "setup",
                "--address",
                "203.0.113.10",
                "--dest",
                "www.example.com",
                "--bot-token",
                "1:abc",
                "--admin",
                "7",
                "--yes",
            ]
        )
        == 0
    )
    assert main(["user", "add", "alice"]) == 0
    link = capsys.readouterr().out
    assert "vless://alice" not in link
    assert "vless://" in link
    assert "#alice" in link
    assert main(["user", "add", "alice"]) == 1
    assert main(["user", "disable", "alice"]) == 0
    assert main(["config"]) == 0
    config = capsys.readouterr().out
    private = Store(home / "state.db").get("private_key")
    assert private
    assert private not in config
    assert '"privateKey": "***"' in config
    assert main(["status"]) == 0
    status = capsys.readouterr().out
    assert "203.0.113.10" in status
    assert "Демон: не запущен" in status
    assert private not in status


def test_cdn_dest_is_rejected_before_dns(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "setup",
            "--address",
            "203.0.113.10",
            "--dest",
            "www.cloudflare.com",
            "--allow-unresolved",
            "--yes",
        ]
    )
    assert code == 1
    assert "CDN" in capsys.readouterr().err


def test_firewall_and_unit_are_printable(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["firewall", "--ssh-port", "22", "--port", "443"]) == 0
    rules = capsys.readouterr().out
    assert "policy drop" in rules
    assert "tcp dport 22 accept" in rules
    assert main(["install", "--print"]) == 0
    unit = capsys.readouterr().out
    assert "ExecStart=" in unit
    assert unit.strip().endswith("WantedBy=multi-user.target")
    assert "serve" in unit
