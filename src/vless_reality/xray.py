from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

from vless_reality.errors import VRError

XRAY_VERSION = "26.10.21"
XRAY_RELEASE = "https://github.com/XTLS/Xray-core/releases/download"


def asset_name(system: str | None = None, machine: str | None = None) -> str:
    system_name = (system or platform.system()).lower()
    machine_name = (machine or platform.machine()).lower()
    table = {
        ("linux", "x86_64"): "Xray-linux-64.zip",
        ("linux", "amd64"): "Xray-linux-64.zip",
        ("linux", "aarch64"): "Xray-linux-arm64-v8a.zip",
        ("linux", "arm64"): "Xray-linux-arm64-v8a.zip",
        ("darwin", "x86_64"): "Xray-macos-64.zip",
        ("darwin", "arm64"): "Xray-macos-arm64-v8a.zip",
        ("windows", "amd64"): "Xray-windows-64.zip",
        ("windows", "x86_64"): "Xray-windows-64.zip",
        ("windows", "arm64"): "Xray-windows-arm64-v8a.zip",
    }
    try:
        return table[(system_name, machine_name)]
    except KeyError:
        raise VRError(f"нет сборки Xray для {system_name}/{machine_name}") from None


def binary_name() -> str:
    return "xray.exe" if os.name == "nt" else "xray"


def release_urls(asset: str, version: str = XRAY_VERSION) -> tuple[str, str]:
    base = f"{XRAY_RELEASE}/v{version}/{asset}"
    return base, base + ".dgst"


def sha256_from_dgst(text: str) -> str:
    for line in text.splitlines():
        if not line.upper().startswith("SHA256="):
            continue
        digest = line.split("=", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return digest
    raise VRError("в файле .dgst нет строки SHA256")


def extract_xray(blob: bytes, dest: Path) -> None:
    wanted = {"xray", "xray.exe"}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if Path(name).name in wanted and not name.endswith("/")]
        if len(names) != 1:
            raise VRError("в архиве Xray нет единственного бинарника xray")
        data = archive.read(names[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    if os.name != "nt":
        os.chmod(dest, 0o755)


def fetch_xray(dest: Path, *, opener=urllib.request.urlopen, version: str = XRAY_VERSION) -> None:
    asset = asset_name()
    archive_url, digest_url = release_urls(asset, version)
    digest_text = _read_url(digest_url, opener, timeout=60).decode()
    expected = sha256_from_dgst(digest_text)
    blob = _read_url(archive_url, opener, timeout=180)
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise VRError("контрольная сумма Xray не сошлась, файл отброшен")
    extract_xray(blob, dest)


def _read_url(url: str, opener, timeout: int) -> bytes:
    try:
        with opener(url, timeout=timeout) as response:
            return response.read()
    except OSError as exc:
        raise VRError(f"не удалось скачать {url}: {exc}") from None


class XrayProcess:
    def __init__(self, binary: str, config_path: Path, log_path: Path, args: list[str] | None = None):
        self.binary = binary
        self.config_path = config_path
        self.log_path = log_path
        self.args = args
        self.proc: subprocess.Popen | None = None
        self._log = None
        self.started_at = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def pid(self) -> int | None:
        if self.proc is None or self.proc.poll() is not None:
            return None
        return self.proc.pid

    def start(self) -> None:
        if self.running():
            return
        if not Path(self.binary).exists() and shutil.which(self.binary) is None:
            raise VRError(f"бинарник Xray не найден: {self.binary}")
        if self._log is not None:
            self._log.close()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "ab", buffering=0)
        command = self.args or [self.binary, "run", "-config", str(self.config_path)]
        self.proc = subprocess.Popen(command, stdout=self._log, stderr=subprocess.STDOUT)
        self.started_at = time.monotonic()

    def stop(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def restart(self) -> None:
        self.stop()
        self.start()
