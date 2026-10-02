# MAX-Forwarder
> [!WARNING]
> Может блокировать аккаунт

Форвардит сообщения из группы [Max](https://max.ru) в Telegram и обратно. MAX — через userbot на [PyMax](https://github.com/MaxApiTeam/PyMax) (без официального бот-API), Telegram — через обычного бота.

Можно запускать одним процессом на одной машине, а можно разнести на две:
**Telegram-половина за рубежом, MAX-половина в РФ**. Код один, кто есть кто задаётся в `config.py`.

> Вдохновлен идеей от [Sharkow1743/SferumTransferBot](https://github.com/Sharkow1743/SferumTransferBot)

## Что умеет

| | MAX → Telegram | Telegram → MAX |
|---|---|---|
| Текст + форматирование (жирный, курсив, ссылки, код…) | ✅ | ✅ |
| Фото | ✅ | ✅ |
| Видео | ✅ | ✅ (GIF уходят как видео) |
| Кружки (видеосообщения) | ✅ | ✅ (перекодируются ffmpeg) |
| Голосовые | ✅ | ✅ (перекодируются ffmpeg) |
| Файлы, музыка | ✅ | ✅ (музыка — файлом) |
| Стикеры | картинкой | статичные — картинкой, анимированные — текстом |
| Ответы на сообщения | ✅ | ✅ |
| Пересланные сообщения | ✅ с пометкой «Переслано от…» | ✅ с пометкой |
| Опросы, контакты, геопозиция | текстом | текстом |

- Кастомные имена для пользователей MAX и Telegram
- Режим `--mute` (только MAX → TG)
- Догоняет пропущенные сообщения MAX после переподключения
- Ограничение Telegram-ботов: файлы из TG больше 20 МБ бот скачать не может, в TG — не больше 50 МБ

## Как это устроено

```
ROLE = "both" — одна машина:

  Telegram ⇄ [ TG-половина ⇄ MAX-половина ] ⇄ MAX

ROLE = "tg" + ROLE = "max" — две машины:

  Telegram ⇄ [ TG-половина ] ⇄── wss + секрет ──⇄ [ MAX-половина ] ⇄ MAX
              сервер за рубежом                    машина в РФ
              (LINK_LISTEN)                        (LINK_URL)
```

- Токен Telegram-бота живёт только на зарубежной машине, сессия MAX — только на российской.
- Медиа качаются на той стороне, где они доступны, и едут по связке байтами.
- Если связь между нодами пропала, сообщения копятся в очереди и досылаются после переподключения.

## Требования

- Python 3.11+
- `ffmpeg` на MAX-машине — без него голосовые и кружки из Telegram в MAX могут не пройти
- Аккаунт в MAX
- Telegram-бот (через [@BotFather](https://t.me/BotFather))

## Установка

```bash
git clone https://github.com/AtTheMoro/MAX-Forwarder
cd MAX-Forwarder
pip install -r requirements.txt
sudo apt install ffmpeg      # Debian/Ubuntu; на macOS: brew install ffmpeg
```

## Настройка

Все настройки в `config.py`, каждая подписана. Минимум зависит от роли:

| Роль | Что заполнить |
|---|---|
| `both` | `MAX_*`, `TG_*` |
| `tg` (за рубежом) | `TG_TOKEN`, `TG_CHAT_ID`, `LINK_*` |
| `max` (в РФ) | `MAX_*`, `LINK_*` |

### 1. Вход в MAX

Три варианта, `MAX_AUTH` в конфиге:

- **`qr`** (по умолчанию, если `MAX_TOKEN` пустой). При первом запуске в консоли появится QR-код — отсканируй его в приложении MAX: **Настройки → Устройства → Войти по QR-коду**.
- **`token`** — токен из веб-версии:
  1. Зайди на [web.max.ru](https://web.max.ru) и авторизуйся
  2. Открой DevTools (`F12`) → **Хранилище** (Storage) → **Локальное хранилище** → `https://web.max.ru`
  3. Найди ключ `__oneme_auth` и скопируй значение поля `token` в `MAX_TOKEN`
- **`sms`** — вход по SMS как с телефона, нужен `MAX_PHONE`. Код спросит в консоли.

Сессия сохраняется в папку `max_session/`, дальше вход не нужен. Чтобы перелогиниться, удали эту папку.

### 2. Telegram-бот

1. [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен в `TG_TOKEN`
2. Добавь бота в группу
3. Отключи Privacy Mode: BotFather → `/mybots` → Bot Settings → Group Privacy → Turn off
4. Напиши в группе `/chatid` — бот ответит ID чата, его в `TG_CHAT_ID`

### 3. ID чата MAX

Открой группу на web.max.ru — в адресной строке будет, например, `https://web.max.ru/-54321098765432`, число и есть `MAX_CHAT_ID`.

## Запуск на одной машине

Подходит, если с машины доступны и MAX, и Telegram (например, Telegram через прокси — `TG_PROXY`).

```bash
python3 main.py            # двусторонний режим
python3 main.py --mute     # только MAX → Telegram
```

## Запуск на двух машинах (TG за рубежом, MAX в РФ)

Обычно слушает зарубежный сервер (у него белый IP), а машина в РФ сама к нему подключается — тогда в РФ белый IP не нужен.

**1. Общий секрет** (один и тот же на обеих машинах):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**2. Зарубежный сервер, TLS-сертификат** для шифрования связки:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout link.key -out link.crt -subj "/CN=max-forwarder"
```

`link.crt` скопируй на машину в РФ (`link.key` — никуда не копировать).

**3. `config.py` на зарубежном сервере:**

```python
ROLE = "tg"
TG_TOKEN = "1234567890:AAF..."
TG_CHAT_ID = "-100123456789"
LINK_LISTEN = "0.0.0.0:8765"
LINK_SECRET = "тот-самый-секрет"
LINK_TLS_CERT = "link.crt"
LINK_TLS_KEY = "link.key"
```

Открой порт в фаерволе: `sudo ufw allow 8765/tcp`. Если связку режут — повесь на `443`.

**4. `config.py` на машине в РФ:**

```python
ROLE = "max"
MAX_CHAT_ID = -54321098765432
MAX_TOKEN = ""            # пусто = вход по QR
LINK_URL = "wss://IP_ЗАРУБЕЖНОГО_СЕРВЕРА:8765/link"
LINK_SECRET = "тот-самый-секрет"
LINK_TLS_CA = "link.crt"  # доверяем только своему сертификату
```

**5. Запуск** — на каждой машине просто `python3 main.py` (роль берётся из конфига, либо `--role tg` / `--role max`). Порядок запуска не важен, MAX-нода будет стучаться, пока не подключится. В логах обеих появится «Связь с …-нодой есть».

Если наоборот белый IP только у машины в РФ — поменяй местами: `LINK_LISTEN` + сертификат на ней, `LINK_URL` + `LINK_TLS_CA` на зарубежной.

## Кастомные имена

### Пользователи MAX (`custom_names.json`)

Переопределяет автоматически определённые имена. ID берётся из `names.json`, который заполняется сам. Файл нужен на MAX-машине.

```json
{
    "211565325": "Никита Моров"
}
```

### Пользователи Telegram (`tg_names.json`)

Имя, которое будет показываться в MAX, когда человек пишет из TG. Файл нужен на TG-машине. ID можно узнать через [@userinfobot](https://t.me/userinfobot).

```json
{
    "_comment": "TG user id → имя в MAX",
    "7213661210": "Никита"
}
```

## Автозапуск (Linux systemd)

Первый запуск MAX-ноды с входом по QR/SMS сделай руками в консоли, дальше — сервисом.

```ini
# ~/.config/systemd/user/maxforwarder.service
[Unit]
Description=MaxForwarder
After=network-online.target

[Service]
WorkingDirectory=/home/ТВОЙ_ЮЗЕР/MAX-Forwarder
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now maxforwarder
loginctl enable-linger $USER   # чтобы работало без активной сессии
journalctl --user -u maxforwarder -f
```

## Заметки

- PyMax закреплён на версии `2.4.1`. В ней сломана загрузка голосовых и фото, поэтому в `forwarder/pymax_patches.py` лежат заплатки. Причины багов нашли в [MAX2TG-Bridge](https://github.com/miharoot/MAX2TG-Bridge) и в issues [PyMax#102](https://github.com/MaxApiTeam/PyMax/issues/102), [PyMax#103](https://github.com/MaxApiTeam/PyMax/issues/103). Не обновляй PyMax, не проверив, что они ещё нужны.
- Правки и удаления сообщений не пересылаются.
- Эхо собственных пересланных сообщений отсекается автоматически, `MAX_SELF_ID` больше не обязателен.
