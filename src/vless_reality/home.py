from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    override = os.environ.get("VR_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "vless-reality"
    return Path("/var/lib/vless-reality")


def restrict_file(path: Path) -> None:
    if os.name == "nt":
        return
    os.chmod(path, 0o600)


def restrict_dir(path: Path) -> None:
    if os.name == "nt":
        return
    os.chmod(path, 0o700)
