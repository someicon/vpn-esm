from __future__ import annotations

import io
import json
import secrets
import urllib.error
import urllib.request
from typing import Protocol

import segno

from vless_reality.errors import VRError
from vless_reality.service import Service

HELP = """\
Команды:
/status — состояние сервера
/users — список пользователей
/add имя — создать и прислать ссылку с QR
/link имя — ссылка и QR ещё раз
/disable имя — отключить
/enable имя — включить
/revoke имя — удалить
/dest домен[:порт] — сменить сайт-прикрытие
/reload — перечитать конфиг и перезапустить Xray
/whoami — ваш Telegram id
"""

COMMANDS = [
    {"command": "status", "description": "Состояние сервера"},
    {"command": "users", "description": "Список пользователей"},
    {"command": "add", "description": "Добавить пользователя"},
    {"command": "link", "description": "Ссылка и QR"},
    {"command": "disable", "description": "Отключить пользователя"},
    {"command": "enable", "description": "Включить пользователя"},
    {"command": "revoke", "description": "Удалить пользователя"},
    {"command": "dest", "description": "Сменить сайт-прикрытие"},
    {"command": "reload", "description": "Перезапустить Xray"},
    {"command": "help", "description": "Список команд"},
]


class Transport(Protocol):
    def send_message(self, chat_id: int, text: str) -> None: ...

    def send_photo(self, chat_id: int, png: bytes, caption: str) -> None: ...

    def get_updates(self, offset: int, timeout: int) -> list[dict]: ...

    def set_commands(self, commands: list[dict]) -> None: ...


class TelegramAPI:
    def __init__(self, token: str, opener=urllib.request.urlopen):
        self.token = token
        self.opener = opener
        self.base = f"https://api.telegram.org/bot{token}"

    def send_message(self, chat_id: int, text: str) -> None:
        self._json("sendMessage", {"chat_id": chat_id, "text": text})

    def send_photo(self, chat_id: int, png: bytes, caption: str) -> None:
        content_type, body = encode_photo(chat_id, png, caption)
        self._request("sendPhoto", body, content_type, timeout=30)

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset:
            payload["offset"] = offset
        result = self._json("getUpdates", payload, timeout=timeout + 10)
        return list(result)

    def set_commands(self, commands: list[dict]) -> None:
        self._json("setMyCommands", {"commands": commands})

    def _json(self, method: str, payload: dict, timeout: int = 30):
        return self._request(
            method,
            json.dumps(payload).encode(),
            "application/json",
            timeout,
        )

    def _request(self, method: str, body: bytes, content_type: str, timeout: int):
        request = urllib.request.Request(
            f"{self.base}/{method}",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with self.opener(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise VRError(f"Telegram ответил {exc.code}: {detail}") from None
        except OSError as exc:
            raise VRError(f"Telegram недоступен: {exc}") from None
        if not parsed.get("ok"):
            raise VRError(parsed.get("description") or "ошибка Telegram")
        return parsed.get("result")


def encode_photo(chat_id: int, png: bytes, caption: str) -> tuple[str, bytes]:
    boundary = "vr" + secrets.token_hex(12)
    chunks = [
        _field(boundary, "chat_id", str(chat_id)),
        _field(boundary, "caption", caption),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="photo"; filename="qr.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + png
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def qr_png(link: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(link, error="m").save(buffer, kind="png", scale=6)
    return buffer.getvalue()


def handle_update(update: dict, service: Service, transport: Transport, runtime: str) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return
    text = message.get("text") or ""
    user = message.get("from") or {}
    try:
        chat_id = int(chat["id"])
        user_id = int(user.get("id") or chat_id)
    except (KeyError, TypeError, ValueError):
        return
    settings = service.settings()
    if user_id not in settings.admin_ids:
        if text.startswith("/"):
            transport.send_message(
                chat_id,
                f"Нет доступа. Ваш Telegram id: {user_id}.\n"
                f"На сервере: vless-reality admin add {user_id}",
            )
        return
    command, arg = _command(text)
    try:
        _dispatch(command, arg, chat_id, service, transport, runtime)
    except VRError as exc:
        transport.send_message(chat_id, str(exc))


def drain_outbox(service: Service, transport: Transport) -> None:
    settings = service.settings()
    if not settings.admin_ids:
        return
    for item in service.store.outbox():
        for admin_id in settings.admin_ids:
            transport.send_message(admin_id, item.body)
        service.store.drop_outbox(item.id)


def _dispatch(command: str, arg: str, chat_id: int, service: Service, transport: Transport, runtime: str) -> None:
    if command == "":
        return
    if command in {"/start", "/help"}:
        transport.send_message(chat_id, HELP)
        return
    if command == "/whoami":
        transport.send_message(chat_id, f"Ваш Telegram id: {chat_id}")
        return
    if command == "/status":
        transport.send_message(chat_id, service.status_text(runtime))
        return
    if command == "/users":
        transport.send_message(chat_id, _users_text(service))
        return
    if command in {"/add", "/link", "/disable", "/enable", "/revoke"} and not arg:
        raise VRError(f"формат: {command} имя")
    if command == "/add":
        user, link = service.add_user(arg, notify=False)
        _send_link(transport, chat_id, f"Пользователь {user.name}", link)
        return
    if command == "/link":
        _send_link(transport, chat_id, arg, service.link_for(arg))
        return
    if command == "/disable":
        service.set_enabled(arg, False, notify=False)
        transport.send_message(chat_id, f"{arg} отключён. Xray перезапустится в течение пары секунд.")
        return
    if command == "/enable":
        service.set_enabled(arg, True, notify=False)
        transport.send_message(chat_id, f"{arg} включён.")
        return
    if command == "/revoke":
        service.remove_user(arg, notify=False)
        transport.send_message(chat_id, f"{arg} удалён.")
        return
    if command == "/dest":
        if not arg:
            raise VRError("формат: /dest домен[:порт]")
        warnings = service.set_dest(arg, notify=False)
        transport.send_message(chat_id, "Прикрытие обновлено.\n" + "\n".join(warnings))
        return
    if command == "/reload":
        service.reload()
        transport.send_message(chat_id, "Перезапуск поставлен в очередь.")
        return
    transport.send_message(chat_id, "Неизвестная команда.\n" + HELP)


def _send_link(transport: Transport, chat_id: int, title: str, link: str) -> None:
    caption = f"{title}\n{link}"
    png = qr_png(link)
    if len(caption) <= 1000:
        transport.send_photo(chat_id, png, caption)
        return
    transport.send_message(chat_id, caption)
    transport.send_photo(chat_id, png, title)


def _users_text(service: Service) -> str:
    users = service.users()
    if not users:
        return "Пользователей нет. /add имя"
    lines = []
    for user in users:
        state = "активен" if user.enabled else "выключен"
        lines.append(f"{user.name} — {state}")
    return "\n".join(lines)


def _command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", ""
    head, _, tail = stripped.partition(" ")
    command = head.split("@", 1)[0].lower()
    return command, tail.strip()


def _field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()
