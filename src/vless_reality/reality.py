from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
import socket
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from vless_reality.errors import VRError
from vless_reality.store import User

FLOW = "xtls-rprx-vision"
FINGERPRINT = "chrome"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)
CDN_SUFFIXES = (
    ".cloudflare.com",
    ".cloudflare.net",
    ".fastly.net",
    ".fastly.com",
    ".cloudfront.net",
    ".akamai.net",
    ".akamaized.net",
    ".akamaihd.net",
)
# Cloudflare and Fastly publish these ranges. A REALITY target on them forwards
# arbitrary SNI into the CDN, which is an open proxy once nginx is gone.
CDN_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "151.101.0.0/16",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


@dataclass(frozen=True)
class KeyPair:
    private_key: str
    public_key: str


@dataclass(frozen=True)
class Endpoint:
    address: str
    port: int
    sni: str
    dest_port: int
    public_key: str
    short_id: str

    @property
    def target(self) -> str:
        return f"{self.sni}:{self.dest_port}"


def generate_keypair() -> KeyPair:
    private = X25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return KeyPair(_b64(private_raw), _b64(public_raw))


def public_from_private(private_key: str) -> str:
    raw = _b64decode(private_key)
    if len(raw) != 32:
        raise VRError("приватный ключ Reality должен быть 32 байта")
    private = X25519PrivateKey.from_private_bytes(raw)
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64(public_raw)


def new_short_id() -> str:
    return secrets.token_hex(4)


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise VRError("имя: 1–32 символа, латиница, цифры, _ и -")
    return name


def validate_address(address: str) -> str:
    value = address.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    host = value.lower().rstrip(".")
    if HOST_RE.fullmatch(host):
        return host
    raise VRError("адрес в ссылке — это IP или домен, без схемы и порта")


def validate_port(port: int) -> int:
    if not 1 <= int(port) <= 65535:
        raise VRError("порт вне диапазона 1–65535")
    return int(port)


def parse_dest(value: str) -> tuple[str, int]:
    raw = value.strip()
    if "://" in raw or "/" in raw or not raw:
        raise VRError("dest — это домен[:порт], без схемы и пути")
    host, port = raw, 443
    if raw.count(":") == 1:
        host, port_text = raw.rsplit(":", 1)
        if not port_text.isdigit():
            raise VRError("порт dest должен быть числом")
        port = int(port_text)
    elif raw.count(":") > 1:
        raise VRError("для dest нужен домен, не IPv6")
    host = host.strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(host):
        raise VRError("dest должен быть доменом, например dl.google.com")
    return host, validate_port(port)


def sni_warning(host: str) -> str | None:
    name = host.lower()
    risky = name.endswith((".ru", ".ir", ".cn")) or any(
        word in name for word in ("microsoft", "apple", "icloud")
    )
    if not risky:
        return None
    return (
        "Xray считает такой target опасным для IP сервера. "
        "Лучше сайт из той же сети, что и VPS, и не из CDN."
    )


def resolve_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(str(exc)) from exc
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        ip = ipaddress.ip_address(addr)
        if ip not in found:
            found.append(ip)
    if not found:
        raise OSError("пустой ответ DNS")
    return found


def assert_dest_safe(
    host: str,
    *,
    allow_cdn: bool = False,
    allow_unresolved: bool = False,
    resolve=resolve_ips,
) -> None:
    if host.endswith(CDN_SUFFIXES) or host in {"cloudflare.com", "fastly.com"}:
        if allow_cdn:
            return
        raise VRError(
            "этот домен — CDN. Без отдельного фильтра SNI чужой ClientHello "
            "уйдёт в CDN и откроет через ваш порт чужие сайты. "
            "Выберите сайт со своим адресом или передайте --allow-cdn."
        )
    try:
        ips = resolve(host)
    except OSError as exc:
        if allow_unresolved:
            return
        raise VRError(
            f"не удалось проверить {host}: {exc}. "
            "Повторите позже или передайте --allow-unresolved."
        ) from None
    for ip in ips:
        if any(ip in network for network in CDN_NETWORKS):
            if allow_cdn:
                return
            raise VRError(
                f"{host} резолвится в {ip}, это сеть CDN. "
                "Такой target превращает сервер в открытый форвардер. "
                "Выберите сайт со своим адресом или передайте --allow-cdn."
            )


def spider_x(user_uuid: str) -> str:
    digest = hashlib.sha256(user_uuid.encode()).hexdigest()[:8]
    return f"/{digest}"


def vless_link(user: User, endpoint: Endpoint) -> str:
    host = endpoint.address
    if ":" in host:
        host = f"[{host}]"
    query = (
        f"encryption=none&flow={FLOW}&security=reality&sni={endpoint.sni}"
        f"&fp={FINGERPRINT}&pbk={endpoint.public_key}&sid={endpoint.short_id}"
        f"&spx={_query_escape(spider_x(user.uuid))}&type=tcp&headerType=none"
    )
    return f"vless://{user.uuid}@{host}:{endpoint.port}?{query}#{user.name}"


def xray_config(
    endpoint: Endpoint,
    *,
    private_key: str,
    users: list[User],
    error_log: str,
    min_client_ver: str = "",
) -> dict:
    clients = [
        {"id": user.uuid, "flow": FLOW, "email": user.name}
        for user in users
        if user.enabled
    ]
    reality: dict = {
        "show": False,
        "target": endpoint.target,
        "xver": 0,
        "serverNames": [endpoint.sni],
        "privateKey": private_key,
        "shortIds": ["", endpoint.short_id],
    }
    if min_client_ver:
        reality["minClientVer"] = min_client_ver
    return {
        "log": {"loglevel": "warning", "error": error_log},
        "inbounds": [
            {
                "listen": "0.0.0.0",
                "port": endpoint.port,
                "protocol": "vless",
                "tag": "reality-in",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "raw",
                    "method": "raw",
                    "security": "reality",
                    "realitySettings": reality,
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _query_escape(value: str) -> str:
    return value.replace("%", "%25").replace("/", "%2F").replace("?", "%3F").replace("&", "%26")
