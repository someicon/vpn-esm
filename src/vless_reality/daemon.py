from __future__ import annotations

import json
import logging
import os
import signal
import time
from pathlib import Path

from vless_reality.bot import COMMANDS, TelegramAPI, Transport, drain_outbox, handle_update
from vless_reality.errors import VRError
from vless_reality.service import Service
from vless_reality.xray import XrayProcess

log = logging.getLogger("vless_reality")


class Daemon:
    def __init__(
        self,
        service: Service,
        binary: str,
        home: Path,
        transport: Transport | None = None,
        poll_timeout: int = 2,
        xray_args: list[str] | None = None,
    ):
        self.service = service
        self.home = home
        self.config_path = home / "xray.json"
        self.heartbeat_path = home / "daemon.json"
        self.pid_path = home / "daemon.pid"
        self.transport = transport
        self.poll_timeout = poll_timeout
        self.xray = XrayProcess(binary, self.config_path, home / "xray-stderr.log", args=xray_args)
        self.applied: int | None = None
        self.next_start = 0.0
        self.backoff = 1.0
        self.stop = False

    def run(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        if self.transport is not None:
            try:
                self.transport.set_commands(COMMANDS)
            except VRError as exc:
                log.error("%s", exc)
        self._install_signals()
        while not self.stop:
            try:
                self.tick()
            except VRError as exc:
                log.error("%s", exc)
            if self.transport is None and not self.stop:
                time.sleep(self.poll_timeout)
        self.xray.stop()
        self.pid_path.unlink(missing_ok=True)

    def tick(self) -> None:
        self._sync_xray()
        self._write_heartbeat()
        if self.transport is None:
            return
        try:
            drain_outbox(self.service, self.transport)
        except VRError as exc:
            log.error("%s", exc)
        offset = int(self.service.store.get("tg_offset") or "0")
        updates = self.transport.get_updates(offset, self.poll_timeout)
        for update in updates:
            try:
                handle_update(update, self.service, self.transport, self.runtime_text())
            except VRError as exc:
                log.error("%s", exc)
            update_id = update.get("update_id")
            if update_id is not None:
                self.service.store.set("tg_offset", str(int(update_id) + 1))

    def runtime_text(self) -> str:
        pid = self.xray.pid()
        if pid:
            return f"Xray: работает, pid {pid}"
        return "Xray: остановлен"

    def _sync_xray(self) -> None:
        now = time.monotonic()
        generation = self.service.generation()
        if self.applied != generation:
            if now < self.next_start:
                return
            try:
                self.service.write_xray_config(self.config_path)
                self.xray.restart()
            except VRError as exc:
                self.next_start = time.monotonic() + self.backoff
                self.backoff = min(self.backoff * 2, 30)
                log.error("%s Повтор через %.0f с", exc, self.backoff)
                return
            self.applied = generation
            self.backoff = 1.0
            self.next_start = 0.0
            log.info("конфиг Xray применён, поколение %s", generation)
            return
        if self.xray.proc is not None and not self.xray.running():
            lived = now - self.xray.started_at
            code = self.xray.proc.returncode
            self.xray.proc = None
            if lived < 2:
                self.next_start = now + self.backoff
                self.backoff = min(self.backoff * 2, 30)
                log.error("Xray завершился с кодом %s, повтор через %.0f с", code, self.backoff)
                return
        if self.xray.running():
            return
        if now < self.next_start:
            return
        self.xray.start()
        self.backoff = 1.0

    def _write_heartbeat(self) -> None:
        payload = {
            "pid": os.getpid(),
            "xray_pid": self.xray.pid(),
            "updated_at": time.time(),
        }
        temporary = self.heartbeat_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self.heartbeat_path)

    def _install_signals(self) -> None:
        def _stop(_signum, _frame):
            self.stop = True

        signal.signal(signal.SIGINT, _stop)
        if os.name != "nt":
            signal.signal(signal.SIGTERM, _stop)


def open_transport(token: str) -> TelegramAPI | None:
    if not token:
        return None
    return TelegramAPI(token)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def runtime_from_heartbeat(path: Path, now: float | None = None) -> str:
    if not path.exists():
        return "Демон: не запущен"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        updated = float(data["updated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return "Демон: не запущен"
    current = time.time() if now is None else now
    if current - updated > 20 or not pid_alive(pid):
        return "Демон: не запущен"
    xray_pid = data.get("xray_pid")
    if xray_pid and pid_alive(int(xray_pid)):
        return f"Демон: работает, Xray pid {xray_pid}"
    return "Демон: работает, Xray остановлен"

