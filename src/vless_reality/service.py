from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from vless_reality.errors import VRError
from vless_reality.home import restrict_file
from vless_reality.reality import (
    Endpoint,
    assert_dest_safe,
    generate_keypair,
    new_short_id,
    parse_dest,
    public_from_private,
    resolve_ips,
    sni_warning,
    validate_address,
    validate_name,
    validate_port,
    vless_link,
    xray_config,
)
from vless_reality.store import Store, User


@dataclass(frozen=True)
class Settings:
    address: str
    port: int
    sni: str
    dest_port: int
    private_key: str
    public_key: str
    short_id: str
    bot_token: str
    admin_ids: tuple[int, ...]
    xray_bin: str
    ssh_port: int
    min_client_ver: str

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint(
            address=self.address,
            port=self.port,
            sni=self.sni,
            dest_port=self.dest_port,
            public_key=self.public_key,
            short_id=self.short_id,
        )


class Service:
    def __init__(self, store: Store):
        self.store = store

    def configured(self) -> bool:
        return bool(self.store.get("address") and self.store.get("private_key"))

    def settings(self) -> Settings:
        if not self.configured():
            raise VRError("сервер ещё не настроен: vless-reality setup")
        private_key = self.store.get("private_key") or ""
        public_key = self.store.get("public_key") or ""
        if public_from_private(private_key) != public_key:
            raise VRError("пара ключей Reality в базе повреждена")
        return Settings(
            address=self.store.get("address") or "",
            port=int(self.store.get("port") or "443"),
            sni=self.store.get("sni") or "",
            dest_port=int(self.store.get("dest_port") or "443"),
            private_key=private_key,
            public_key=public_key,
            short_id=self.store.get("short_id") or "",
            bot_token=self.store.get("bot_token") or "",
            admin_ids=self.store.admin_ids(),
            xray_bin=self.store.get("xray_bin") or "",
            ssh_port=int(self.store.get("ssh_port") or "22"),
            min_client_ver=self.store.get("min_client_ver") or "",
        )

    def setup(
        self,
        *,
        address: str,
        dest: str = "dl.google.com:443",
        port: int = 443,
        bot_token: str = "",
        admin_ids: list[int] | None = None,
        ssh_port: int = 22,
        allow_cdn: bool = False,
        allow_unresolved: bool = False,
        xray_bin: str = "",
        resolve=None,
    ) -> list[str]:
        address = validate_address(address)
        port = validate_port(port)
        ssh_port = validate_port(ssh_port)
        sni, dest_port = parse_dest(dest)
        assert_dest_safe(
            sni,
            allow_cdn=allow_cdn,
            allow_unresolved=allow_unresolved,
            resolve=resolve or resolve_ips,
        )
        if not self.store.get("private_key"):
            pair = generate_keypair()
            self.store.set("private_key", pair.private_key)
            self.store.set("public_key", pair.public_key)
        if not self.store.get("short_id"):
            self.store.set("short_id", new_short_id())
        self.store.set("address", address)
        self.store.set("port", str(port))
        self.store.set("sni", sni)
        self.store.set("dest_port", str(dest_port))
        self.store.set("ssh_port", str(ssh_port))
        if bot_token:
            self.store.set("bot_token", bot_token.strip())
        if admin_ids:
            self.store.set_admin_ids([int(item) for item in admin_ids])
        if xray_bin:
            self.store.set("xray_bin", xray_bin)
        self.store.bump_reload()
        warnings = []
        warning = sni_warning(sni)
        if warning:
            warnings.append(warning)
        if self.store.get("bot_token") and not self.store.admin_ids():
            warnings.append("токен бота записан, но список админов пуст: vless-reality admin add ID")
        return warnings

    def add_user(self, name: str, *, notify: bool = False) -> tuple[User, str]:
        settings = self.settings()
        name = validate_name(name)
        try:
            user = self.store.add_user(name, str(uuid.uuid4()))
        except ValueError:
            raise VRError(f"пользователь {name} уже есть") from None
        link = vless_link(user, settings.endpoint)
        self.store.bump_reload()
        if notify and settings.bot_token and settings.admin_ids:
            self.store.enqueue(f"Новый пользователь {name}\n{link}")
        return user, link

    def remove_user(self, name: str, *, notify: bool = False) -> None:
        settings = self.settings()
        name = validate_name(name)
        if not self.store.remove_user(name):
            raise VRError(f"пользователя {name} нет")
        self.store.bump_reload()
        if notify and settings.bot_token and settings.admin_ids:
            self.store.enqueue(f"Пользователь {name} удалён")

    def set_enabled(self, name: str, enabled: bool, *, notify: bool = False) -> None:
        settings = self.settings()
        name = validate_name(name)
        if not self.store.set_enabled(name, enabled):
            raise VRError(f"пользователя {name} нет")
        self.store.bump_reload()
        if notify and settings.bot_token and settings.admin_ids:
            state = "включён" if enabled else "отключён"
            self.store.enqueue(f"Пользователь {name} {state}")

    def set_dest(
        self,
        dest: str,
        *,
        allow_cdn: bool = False,
        allow_unresolved: bool = False,
        notify: bool = False,
        resolve=None,
    ) -> list[str]:
        settings = self.settings()
        sni, dest_port = parse_dest(dest)
        assert_dest_safe(
            sni,
            allow_cdn=allow_cdn,
            allow_unresolved=allow_unresolved,
            resolve=resolve or resolve_ips,
        )
        self.store.set("sni", sni)
        self.store.set("dest_port", str(dest_port))
        self.store.bump_reload()
        if notify and settings.bot_token and settings.admin_ids:
            self.store.enqueue(
                f"Сайт-прикрытие сменён на {sni}:{dest_port}. Старые ссылки нужно выдать заново."
            )
        warnings = []
        warning = sni_warning(sni)
        if warning:
            warnings.append(warning)
        warnings.append("SNI в ссылках изменился, разошлите их заново.")
        return warnings

    def link_for(self, name: str) -> str:
        settings = self.settings()
        name = validate_name(name)
        user = self.store.user(name)
        if user is None:
            raise VRError(f"пользователя {name} нет")
        if not user.enabled:
            raise VRError(f"пользователь {name} отключён")
        return vless_link(user, settings.endpoint)

    def users(self) -> list[User]:
        self.settings()
        return self.store.users()

    def add_admin(self, admin_id: int) -> None:
        self.settings()
        if admin_id <= 0:
            raise VRError("Telegram id должен быть положительным числом")
        ids = list(self.store.admin_ids())
        if admin_id not in ids:
            ids.append(admin_id)
        self.store.set_admin_ids(ids)

    def remove_admin(self, admin_id: int) -> None:
        self.settings()
        ids = [item for item in self.store.admin_ids() if item != admin_id]
        if len(ids) == len(self.store.admin_ids()):
            raise VRError(f"админа {admin_id} нет")
        self.store.set_admin_ids(ids)

    def reload(self) -> None:
        self.settings()
        self.store.bump_reload()

    def generation(self) -> int:
        return self.store.generation()

    def write_xray_config(self, path: Path) -> None:
        settings = self.settings()
        document = xray_config(
            settings.endpoint,
            private_key=settings.private_key,
            users=self.store.users(),
            error_log=str(path.parent / "xray.log"),
            min_client_ver=settings.min_client_ver,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        restrict_file(temporary)
        temporary.replace(path)

    def status_text(self, runtime: str) -> str:
        settings = self.settings()
        users = self.store.users()
        enabled = sum(1 for user in users if user.enabled)
        lines = [
            f"Адрес: {settings.address}:{settings.port}",
            f"Прикрытие: {settings.sni}:{settings.dest_port}",
            f"Public key: {settings.public_key}",
            f"Short ID: {settings.short_id}",
            f"Пользователи: {enabled} активных, {len(users) - enabled} отключённых",
            f"Telegram: {'настроен' if settings.bot_token else 'выключен'}",
            f"Админы: {', '.join(str(item) for item in settings.admin_ids) or 'нет'}",
            runtime,
        ]
        return "\n".join(lines)
