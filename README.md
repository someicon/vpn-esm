# vless-reality

VLESS+Reality на одном VPS. Настройка живёт на этом же сервере: короткая команда `vless-reality` и Telegram-бот. Отдельная машина, веб-панель, Docker, nginx и Postgres не используются.

Xray слушает 443/TCP сам. Клиент с ключом получает туннель. Остальные handshake уходят на настоящий сайт, и снаружи виден его сертификат. Свой сертификат здесь не выпускается: ссылка уходит в Telegram, публичной страницы подписки нет.

Ориентир по устройству — [Meridian](https://github.com/getmeridian/meridian). От него сознательно оставлены только Reality, пользователи и выдача ссылки.

## Что нужно

- Linux VPS, Debian или Ubuntu, Python 3.11+
- исходящий доступ к `api.telegram.org`, если нужен бот
- порт 443 свободен
- бот, которого создал [@BotFather](https://t.me/BotFather), и ваш числовой Telegram id

На Windows можно собрать ссылки и прогнать тесты. Боевой процесс рассчитан на VPS.

## Установка

```bash
sudo apt-get update
sudo apt-get install -y python3-venv nftables
sudo python3 -m venv /opt/vless-reality
sudo /opt/vless-reality/bin/pip install .
sudo mkdir -p /var/lib/vless-reality
sudo /opt/vless-reality/bin/vless-reality fetch-xray
```

`fetch-xray` берёт [Xray-core 26.10.21](https://github.com/XTLS/Xray-core/releases/tag/v26.10.21) и проверяет SHA256 по файлу `.dgst`.

## Первый запуск

```bash
sudo /opt/vless-reality/bin/vless-reality setup \
  --address 203.0.113.10 \
  --dest dl.google.com:443 \
  --bot-token "$VR_BOT_TOKEN" \
  --admin 123456789
sudo /opt/vless-reality/bin/vless-reality install
sudo systemctl enable --now vless-reality
sudo /opt/vless-reality/bin/vless-reality user add alice
```

`--address` — то, что попадёт в ссылку. `--dest` — чужой сайт, чей сертификат увидит проверка без ключа. Берите сайт из той же сети, что и VPS, и не из CDN.

Повторный `setup` не меняет ключи и пользователей.

Токен можно не класть в историю оболочки: переменная `VR_BOT_TOKEN` или запрос при запуске без `--yes`.

## Telegram

Бот отвечает только админам и только в личке. Чужому `/start` он сообщает его id, чтобы этого человека можно было добавить:

```bash
vless-reality admin add 123456789
```

Команды бота: `/add`, `/link`, `/users`, `/disable`, `/enable`, `/revoke`, `/dest`, `/status`, `/reload`.

`/add имя` присылает `vless://` и QR. То же самое делает `vless-reality user add`: ссылка печатается на сервере и уходит админам в Telegram, когда бот уже запущен.

## Сайт-прикрытие

Без nginx Xray сам пересылает неуспешный handshake на `target`. Если этот адрес принадлежит общему CDN, чужой SNI выбирает на CDN произвольный сайт, и ваш порт становится открытым форвардером. Поэтому домены CDN и адреса Cloudflare и Fastly отклоняются. Сети Akamai и CloudFront в проверку входят не целиком: перед запуском убедитесь, что IP сайта принадлежит ему самому. Обойти отказ можно только явным `--allow-cdn`.

Xray дополнительно предупреждает о target с `microsoft`, `apple`, `icloud` и о зонах `.ru`, `.ir`, `.cn`. Такие имена принимаются, но в ответе будет предупреждение.

Лимит скорости fallback не включён: у Xray это отдельный отпечаток, а не защита.

## Файрвол

```bash
vless-reality firewall --ssh-port 22
vless-reality firewall --ssh-port 22 --yes
```

Без `--yes` команда только печатает правила nftables. С `--yes` таблица `inet vless_reality` принимает SSH, 443/TCP, loopback и уже установленные соединения, остальной вход отбрасывает. Перед `--yes` откройте вторую SSH-сессию. UDP 443 не открывается: Hysteria2 в этой версии нет.

## Каталог

По умолчанию `/var/lib/vless-reality` (`VR_HOME`, если задан):

- `state.db` — ключи Reality, пользователи, токен бота
- `xray.json` — собранный конфиг, появляется после старта демона
- `bin/xray` — скачанное ядро

Каталог и база закрыты правами `0700`/`0600`. Копия этого каталога — полный бэкап. Приватный ключ в `vless-reality config` скрыт; `--show-private` показывает его.

Демон читает базу примерно раз в две секунды. `user add`, `/add` и `reload` ставят перезапуск Xray в очередь. Короткий обрыв текущих сессий при добавлении пользователя — нормальное поведение этой версии. Unit не задаёт отдельного пользователя: процесс слушает 443 и поэтому работает от root.

## Клиенты

Ссылка рассчитана на приложения, которые понимают VLESS, Reality и Vision: Happ, v2rayNG, Streisand и похожие. В ссылке `type=tcp`: так клиенты называют транспорт, который в Xray 26 называется RAW. Отпечаток ClientHello — Chrome.

## Сертификаты

Своего TLS-сертификата нет. Reality не предъявляет сертификат сервера, он показывает сертификат сайта-прикрытия. Let's Encrypt понадобится, когда появится свой HTTPS: страница подписки или XHTTP. Выпускать его будет этот же процесс, не nginx.

## Чего нет

Реле, несколько exit, Hysteria2, XHTTP, подписка с автообновлением, веб-панель и раздача сертификатов. Это следующий слой, не эта версия.

## Разработка

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\pytest
```

На Linux те же команды через `bin` вместо `Scripts`.
