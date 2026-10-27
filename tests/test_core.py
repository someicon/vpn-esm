from __future__ import annotations

import base64
import io
import ipaddress
import zipfile
from pathlib import Path

import pytest

from vless_reality.errors import VRError
from vless_reality.ops import render_nftables, render_systemd
from vless_reality.reality import (
    assert_dest_safe,
    generate_keypair,
    parse_dest,
    public_from_private,
    sni_warning,
    vless_link,
    xray_config,
)
from vless_reality.service import Service
from vless_reality.xray import asset_name, extract_xray, sha256_from_dgst


def _pad(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _resolve(_host: str):
    return [ipaddress.ip_address("203.0.113.10")]


def test_keypair_is_raw_x25519() -> None:
    pair = generate_keypair()
    assert len(_pad(pair.private_key)) == 32
    assert len(_pad(pair.public_key)) == 32
    assert public_from_private(pair.private_key) == pair.public_key


def test_link_carries_reality_fields(service: Service) -> None:
    user, link = service.add_user("alice")
    settings = service.settings()
    assert link.startswith(f"vless://{user.uuid}@203.0.113.10:443?")
    assert "encryption=none" in link
    assert "flow=xtls-rprx-vision" in link
    assert "security=reality" in link
    assert "sni=www.example.com" in link
    assert "fp=chrome" in link
    assert f"pbk={settings.public_key}" in link
    assert f"sid={settings.short_id}" in link
    assert "type=tcp" in link
    assert link.endswith("#alice")


def test_ipv6_address_is_bracketed(service: Service) -> None:
    service.store.set("address", "2001:db8::10")
    link = service.link_for("alice") if service.store.user("alice") else service.add_user("alice")[1]
    assert "@[2001:db8::10]:443?" in link


def test_xray_config_keeps_only_enabled_clients(service: Service) -> None:
    service.add_user("alice")
    service.add_user("bob")
    service.set_enabled("bob", False)
    settings = service.settings()
    document = xray_config(
        settings.endpoint,
        private_key=settings.private_key,
        users=service.users(),
        error_log="/var/lib/vless-reality/xray.log",
    )
    inbound = document["inbounds"][0]
    reality = inbound["streamSettings"]["realitySettings"]
    assert inbound["streamSettings"]["network"] == "raw"
    assert inbound["streamSettings"]["method"] == "raw"
    assert inbound["settings"]["decryption"] == "none"
    assert reality["target"] == "www.example.com:443"
    assert reality["serverNames"] == ["www.example.com"]
    assert reality["shortIds"][0] == ""
    assert settings.short_id in reality["shortIds"]
    assert "minClientVer" not in reality
    assert [item["email"] for item in inbound["settings"]["clients"]] == ["alice"]
    built = vless_link(service.store.user("alice"), settings.endpoint)
    assert "www.example.com" in built


def test_duplicate_user_is_rejected(service: Service) -> None:
    service.add_user("alice")
    with pytest.raises(VRError, match="уже есть"):
        service.add_user("alice")


def test_dest_change_rewrites_sni(service: Service) -> None:
    service.add_user("alice")
    service.set_dest("updates.example.com:8443", resolve=_resolve)
    assert "sni=updates.example.com" in service.link_for("alice")
    assert service.settings().dest_port == 8443


def test_cdn_target_is_refused() -> None:
    with pytest.raises(VRError, match="CDN"):
        assert_dest_safe("www.cloudflare.com")
    with pytest.raises(VRError, match="CDN"):
        assert_dest_safe("www.example.com", resolve=lambda _host: [ipaddress.ip_address("104.16.0.1")])
    assert_dest_safe("www.example.com", resolve=_resolve)


def test_sni_warning_matches_xray() -> None:
    assert sni_warning("www.microsoft.com")
    assert sni_warning("www.example.com") is None
    assert parse_dest("WWW.Example.com:8443") == ("www.example.com", 8443)


def test_nftables_keeps_ssh_and_reality() -> None:
    rules = render_nftables(2222, 443)
    assert "tcp dport 2222 accept" in rules
    assert "tcp dport 443 accept" in rules
    assert "policy drop" in rules
    assert 'iif "lo" accept' in rules


def test_systemd_quotes_paths_with_spaces() -> None:
    unit = render_systemd([r"C:\Program Files\vless-reality.exe"], Path(r"C:\Program Files\data"))
    assert r'ExecStart="C:\Program Files\vless-reality.exe" serve' in unit
    assert r'Environment=VR_HOME="C:\Program Files\data"' in unit


def test_xray_archive_extracts_binary_not_its_path(tmp_path: Path) -> None:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("nested/../../xray", b"xray-binary")
    dest = tmp_path / "bin" / "xray"
    extract_xray(blob.getvalue(), dest)
    assert dest.read_bytes() == b"xray-binary"
    assert not (tmp_path / "xray").exists()


def test_xray_archive_rejects_two_binaries() -> None:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("xray", b"a")
        archive.writestr("other/xray", b"b")
    with pytest.raises(VRError):
        extract_xray(blob.getvalue(), Path("xray"))


def test_release_metadata() -> None:
    digest = "ab" * 32
    assert sha256_from_dgst(f"MD5= ff\nSHA256= {digest}\n") == digest
    assert asset_name("linux", "x86_64") == "Xray-linux-64.zip"
    assert asset_name("windows", "amd64") == "Xray-windows-64.zip"
    with pytest.raises(VRError):
        asset_name("plan9", "amd64")
