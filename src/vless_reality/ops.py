from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from vless_reality.errors import VRError


def render_nftables(ssh_port: int, listen_port: int) -> str:
    ports = sorted({int(ssh_port), int(listen_port)})
    port_rules = "\n".join(f"    tcp dport {port} accept" for port in ports)
    return f"""table inet vless_reality {{
  chain input {{
    type filter hook input priority -10; policy drop;
    ct state established,related accept
    iif "lo" accept
{port_rules}
    icmp type echo-request accept
    icmpv6 type {{ echo-request, nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert }} accept
  }}
}}
"""


def apply_nftables(rules: str) -> None:
    if os.name == "nt" or shutil.which("nft") is None:
        raise VRError("nft есть только на Linux с установленным nftables")
    subprocess.run(
        ["nft", "delete", "table", "inet", "vless_reality"],
        check=False,
        capture_output=True,
    )
    result = subprocess.run(["nft", "-f", "-"], input=rules.encode(), capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise VRError(detail or "nft не принял правила")


def render_systemd(argv: list[str], home: Path) -> str:
    command = " ".join(_quote(part) for part in [*argv, "serve"])
    return f"""[Unit]
Description=VLESS Reality
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=VR_HOME={_quote(str(home))}
ExecStart={command}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def install_systemd(unit: str, systemctl: bool = True) -> Path:
    if os.name == "nt":
        raise VRError("systemd есть только на Linux")
    path = Path("/etc/systemd/system/vless-reality.service")
    try:
        path.write_text(unit, encoding="utf-8")
    except OSError as exc:
        raise VRError(f"не удалось записать unit: {exc}") from None
    if systemctl and shutil.which("systemctl"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "vless-reality"], check=False)
    return path


def service_argv() -> list[str]:
    found = shutil.which("vless-reality")
    if found:
        return [found]
    return [sys.executable, "-m", "vless_reality"]


def _quote(value: str) -> str:
    if re.search(r"[\s\"]", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value
