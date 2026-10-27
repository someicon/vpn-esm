from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from vless_reality.daemon import Daemon, open_transport, runtime_from_heartbeat
from vless_reality.errors import VRError
from vless_reality.home import home_dir
from vless_reality.ops import apply_nftables, install_systemd, render_nftables, render_systemd, service_argv
from vless_reality.reality import xray_config
from vless_reality.service import Service
from vless_reality.store import Store
from vless_reality.xray import binary_name, fetch_xray


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except VRError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vless-reality",
        description="VLESS+Reality на этом VPS. Панель, nginx и отдельная машина не нужны.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="первичная настройка или обновление адреса, бота и прикрытия")
    setup.add_argument("--address", help="публичный IP или домен, который попадёт в ссылку")
    setup.add_argument("--dest", help="сайт-прикрытие, домен[:порт], по умолчанию dl.google.com:443")
    setup.add_argument("--port", type=int, help="порт Xray, по умолчанию 443")
    setup.add_argument("--ssh-port", type=int, help="SSH-порт для файла правил, по умолчанию 22")
    setup.add_argument("--bot-token", help="токен Telegram-бота; можно через VR_BOT_TOKEN")
    setup.add_argument("--admin", action="append", type=int, help="Telegram id админа, можно повторять")
    setup.add_argument("--xray-bin", help="путь к бинарнику Xray")
    setup.add_argument("--allow-cdn", action="store_true", help="разрешить target в сети CDN")
    setup.add_argument("--allow-unresolved", action="store_true", help="не проверять DNS target")
    setup.add_argument("--yes", action="store_true", help="не задавать вопросы")
    setup.set_defaults(func=cmd_setup)

    fetch = sub.add_parser("fetch-xray", help="скачать официальный Xray в каталог данных")
    fetch.set_defaults(func=cmd_fetch)

    serve = sub.add_parser("serve", help="держать Xray и Telegram-бота")
    serve.add_argument("--bin", help="путь к Xray, если он не в каталоге данных и не в PATH")
    serve.set_defaults(func=cmd_serve)

    user = sub.add_parser("user", help="пользователи")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    add = user_sub.add_parser("add")
    add.add_argument("name")
    add.set_defaults(func=cmd_user_add)
    show = user_sub.add_parser("show")
    show.add_argument("name")
    show.set_defaults(func=cmd_user_show)
    user_sub.add_parser("list").set_defaults(func=cmd_user_list)
    remove = user_sub.add_parser("remove")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_user_remove)
    disable = user_sub.add_parser("disable")
    disable.add_argument("name")
    disable.set_defaults(func=cmd_user_disable)
    enable = user_sub.add_parser("enable")
    enable.add_argument("name")
    enable.set_defaults(func=cmd_user_enable)

    link = sub.add_parser("link", help="напечатать ссылку пользователя")
    link.add_argument("name")
    link.set_defaults(func=cmd_user_show)

    dest = sub.add_parser("dest", help="сменить сайт-прикрытие")
    dest.add_argument("dest")
    dest.add_argument("--allow-cdn", action="store_true")
    dest.add_argument("--allow-unresolved", action="store_true")
    dest.set_defaults(func=cmd_dest)

    sub.add_parser("reload", help="попросить демон перезапустить Xray").set_defaults(func=cmd_reload)
    sub.add_parser("status", help="показать состояние").set_defaults(func=cmd_status)

    config = sub.add_parser("config", help="показать конфиг Xray")
    config.add_argument("--show-private", action="store_true")
    config.set_defaults(func=cmd_config)

    admin = sub.add_parser("admin", help="админы Telegram")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    admin_add = admin_sub.add_parser("add")
    admin_add.add_argument("telegram_id", type=int)
    admin_add.set_defaults(func=cmd_admin_add)
    admin_remove = admin_sub.add_parser("remove")
    admin_remove.add_argument("telegram_id", type=int)
    admin_remove.set_defaults(func=cmd_admin_remove)
    admin_sub.add_parser("list").set_defaults(func=cmd_admin_list)

    firewall = sub.add_parser("firewall", help="показать или применить правила nftables")
    firewall.add_argument("--ssh-port", type=int)
    firewall.add_argument("--port", type=int)
    firewall.add_argument("--yes", action="store_true", help="применить правила, а не только показать")
    firewall.set_defaults(func=cmd_firewall)

    install = sub.add_parser("install", help="записать systemd unit")
    install.add_argument("--print", action="store_true", help="показать unit и не записывать его")
    install.set_defaults(func=cmd_install)
    return parser


def cmd_setup(args: argparse.Namespace) -> int:
    service = _service()
    address = args.address or service.store.get("address")
    address = _ask(address, args.yes, "Публичный IP или домен: ")
    if args.dest:
        dest = args.dest
    elif service.store.get("sni"):
        dest = f"{service.store.get('sni')}:{service.store.get('dest_port') or '443'}"
    else:
        dest = "dl.google.com:443"
    if not args.dest and sys.stdin.isatty() and not args.yes:
        entered = input(f"Сайт-прикрытие [{dest}]: ").strip()
        dest = entered or dest
    if dest in {"dl.google.com", "dl.google.com:443"}:
        print(
            "Это общее прикрытие. Для маскировки лучше сайт, который реально открывается с IP рядом с VPS.",
            file=sys.stderr,
        )
    token = args.bot_token or os.environ.get("VR_BOT_TOKEN") or ""
    if not token and not service.store.get("bot_token") and sys.stdin.isatty() and not args.yes:
        token = getpass.getpass("Токен бота (пусто — без Telegram): ").strip()
    port = args.port if args.port is not None else int(service.store.get("port") or "443")
    ssh_port = args.ssh_port if args.ssh_port is not None else int(service.store.get("ssh_port") or "22")
    warnings = service.setup(
        address=address,
        dest=dest,
        port=port,
        bot_token=token,
        admin_ids=args.admin,
        ssh_port=ssh_port,
        allow_cdn=args.allow_cdn,
        allow_unresolved=args.allow_unresolved,
        xray_bin=args.xray_bin or "",
    )
    for warning in warnings:
        print(warning, file=sys.stderr)
    settings = service.settings()
    print(f"Готово. Ссылки будут на {settings.address}:{settings.port}")
    print(f"Прикрытие: {settings.sni}:{settings.dest_port}")
    print(f"Каталог: {home_dir()}")
    return 0


def cmd_fetch(_args: argparse.Namespace) -> int:
    dest = home_dir() / "bin" / binary_name()
    fetch_xray(dest)
    _service().store.set("xray_bin", str(dest))
    print(dest)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = _service()
    binary = _xray_binary(service, args.bin)
    transport = open_transport(service.settings().bot_token)
    Daemon(service, binary, home_dir(), transport).run()
    return 0


def cmd_user_add(args: argparse.Namespace) -> int:
    _user, link = _service().add_user(args.name, notify=True)
    print(link)
    return 0


def cmd_user_show(args: argparse.Namespace) -> int:
    print(_service().link_for(args.name))
    return 0


def cmd_user_list(_args: argparse.Namespace) -> int:
    users = _service().users()
    if not users:
        print("пользователей нет")
        return 0
    for user in users:
        state = "on" if user.enabled else "off"
        print(f"{user.name}\t{state}")
    return 0


def cmd_user_remove(args: argparse.Namespace) -> int:
    _service().remove_user(args.name, notify=True)
    print(f"{args.name} удалён")
    return 0


def cmd_user_disable(args: argparse.Namespace) -> int:
    _service().set_enabled(args.name, False, notify=True)
    print(f"{args.name} отключён")
    return 0


def cmd_user_enable(args: argparse.Namespace) -> int:
    _service().set_enabled(args.name, True, notify=True)
    print(f"{args.name} включён")
    return 0


def cmd_dest(args: argparse.Namespace) -> int:
    service = _service()
    warnings = service.set_dest(
        args.dest,
        allow_cdn=args.allow_cdn,
        allow_unresolved=args.allow_unresolved,
        notify=True,
    )
    for warning in warnings:
        print(warning, file=sys.stderr)
    settings = service.settings()
    print(f"{settings.sni}:{settings.dest_port}")
    return 0


def cmd_reload(_args: argparse.Namespace) -> int:
    _service().reload()
    print("перезапуск поставлен в очередь")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    service = _service()
    print(service.status_text(runtime_from_heartbeat(home_dir() / "daemon.json")))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    service = _service()
    settings = service.settings()
    document = xray_config(
        settings.endpoint,
        private_key=settings.private_key,
        users=service.users(),
        error_log=str(home_dir() / "xray.log"),
        min_client_ver=settings.min_client_ver,
    )
    if not args.show_private:
        document["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"] = "***"
    print(json.dumps(document, indent=2))
    return 0


def cmd_admin_add(args: argparse.Namespace) -> int:
    _service().add_admin(args.telegram_id)
    print(args.telegram_id)
    return 0


def cmd_admin_remove(args: argparse.Namespace) -> int:
    _service().remove_admin(args.telegram_id)
    print(f"{args.telegram_id} удалён")
    return 0


def cmd_admin_list(_args: argparse.Namespace) -> int:
    ids = _service().settings().admin_ids
    if not ids:
        print("админов нет")
        return 0
    for admin_id in ids:
        print(admin_id)
    return 0


def cmd_firewall(args: argparse.Namespace) -> int:
    service = _service()
    if service.configured():
        settings = service.settings()
        ssh_port = args.ssh_port or settings.ssh_port
        listen_port = args.port or settings.port
    else:
        ssh_port = args.ssh_port or 22
        listen_port = args.port or 443
    rules = render_nftables(ssh_port, listen_port)
    print(rules, end="")
    if not args.yes:
        print("Правила не применены. Проверьте SSH-порт и повторите с --yes.", file=sys.stderr)
        return 0
    apply_nftables(rules)
    print("правила применены", file=sys.stderr)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    unit = render_systemd(service_argv(), home_dir())
    if args.print:
        print(unit, end="")
        return 0
    path = install_systemd(unit)
    print(path)
    return 0


def _service() -> Service:
    return Service(Store(home_dir() / "state.db"))


def _xray_binary(service: Service, explicit: str | None) -> str:
    if explicit:
        if not Path(explicit).exists():
            raise VRError(f"бинарник Xray не найден: {explicit}")
        return explicit
    settings = service.settings()
    if settings.xray_bin and Path(settings.xray_bin).exists():
        return settings.xray_bin
    bundled = home_dir() / "bin" / binary_name()
    if bundled.exists():
        return str(bundled)
    found = shutil.which("xray") or shutil.which("xray.exe")
    if found:
        return found
    raise VRError("Xray не найден. Сначала выполните vless-reality fetch-xray")


def _ask(value: str | None, yes: bool, label: str) -> str:
    if value:
        return value
    if yes or not sys.stdin.isatty():
        raise VRError("укажите --address")
    entered = input(label).strip()
    if not entered:
        raise VRError("пустое значение")
    return entered


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            continue
