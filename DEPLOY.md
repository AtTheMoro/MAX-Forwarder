# Развёртывание на двух серверах

Схема на два сервера:

- **Сервер A** — за рубежом, IP `A.A.A.A`. Держит Telegram-бота (`ROLE = "tg"`).
- **Сервер B** — в РФ, IP `B.B.B.B`. Держит сессию MAX (`ROLE = "max"`).

Дальше везде подставляй свои IP вместо `A.A.A.A` и `B.B.B.B`.

---

## Как это работает

```
 Telegram ──(Bot API, исходящий)── [ A: TG-нода ] ◀══ wss:8443 ══ [ B: MAX-нода ] ──(исходящий)── MAX
                                     слушает порт      B сам звонит    портов не открывает
```

**B подключается к A, а не наоборот.** На B не открыт ни один входящий порт. На A открыт ровно один, и только для IP `B.B.B.B`. С Telegram и MAX обе ноды тоже общаются только исходящими соединениями.

### Сообщение из MAX в Telegram

1. MAX присылает событие на ноду **B** по постоянному соединению PyMax. PyMax ведёт себя как обычный веб-клиент MAX.
2. B отбрасывает всё лишнее: другие чаты, правки, удаления и эхо собственных сообщений.
3. B узнаёт имя отправителя и скачивает медиа с CDN MAX. Качает только по https и только с хостов из `MAX_MEDIA_HOSTS`.
4. B упаковывает сообщение вместе с файлами и отправляет его на **A** по связке.
5. A подтверждает приём (ack). Пока подтверждения нет, B держит сообщение в очереди и после обрыва отправит его снова. Дубли A отбрасывает.
6. A отправляет сообщение в Telegram через Bot API и запоминает пару «id в MAX ↔ id в Telegram», чтобы потом работали ответы.

### Сообщение из Telegram в MAX

1. Бот на **A** забирает апдейты через long polling, это исходящие запросы. Отвечает только на сообщения из `TG_CHAT_ID`.
2. A скачивает файл с серверов Telegram (до 20 МБ) и отправляет его на **B** по связке.
3. B перекодирует войсы и кружки через ffmpeg, загружает их в MAX и отправляет сообщение.
4. B сообщает A результат:
   - `max_ack` — сообщение ушло, A запоминает id для ответов;
   - `max_fail` — не ушло, бот пишет в чат «⚠️ В MAX не ушло: …».

### Если что-то отвалилось

| Что упало | Что будет |
|---|---|
| Связь A↔B | Сообщения копятся в очереди (в памяти, до 512 МБ) и досылаются после переподключения. B переподключается сам. |
| MAX на B | PyMax переподключается и подтягивает из истории пропущенные сообщения, до 50 штук. |
| Telegram на A | aiogram повторяет запросы сам. |
| Перезапуск процесса | Очередь в памяти теряется. Пропущенное из MAX подтянется из истории, из Telegram — нет. |

---

## Безопасность

| Угроза | Защита |
|---|---|
| Кто-то подключится к связке | Фаервол на A пускает на порт только `B.B.B.B`. Приложение дополнительно сверяет IP с `LINK_ALLOW_IPS` (чужим — 403). Затем проверяет секрет на 256 бит **до** открытия WebSocket (без секрета — 401). |
| Перехват или подмена трафика (MITM) | TLS 1.2+ с **закреплённым** сертификатом: B доверяет только сертификату A и больше никому. Подмена DNS или IP не поможет. Без TLS приложение не стартует. |
| Утечка токенов через git | Настройки лежат в `config.py`, а он в `.gitignore`. В репозитории только шаблон `config.example.py`. |
| Другие пользователи на сервере | Отдельный системный пользователь `maxfwd`. Права на `config.py`, `link.key` и `max_session/` — только владельцу. Если права шире, приложение предупредит при старте. |
| Взлом самого приложения | systemd-песочница: файловая система только на чтение (кроме папки приложения), без повышения привилегий, свой `/tmp`. |
| Чужие ссылки в сообщениях MAX | Медиа качаются только по https с CDN MAX. Редиректы тоже проверяются. |
| Подсунутые данные по связке | Только JSON без pickle и eval. Размеры кадров ограничены. |

**Что эта схема НЕ защищает:**

- **Тексты и файлы на самих серверах лежат открыто.** Кто владеет сервером, тот видит переписку. Бери хостеров, которым доверяешь.
- **Взлом одного сервера бьёт по второму мессенджеру.** Кто захватил A, может писать в чат MAX от имени аккаунта (через B). Кто захватил B, может писать в Telegram от имени бота. Токен бота при этом остаётся на A, а сессия MAX — на B. Что делать в таком случае — в разделе «Если сервер взломали».
- **Аккаунт MAX.** Это userbot, и MAX может его заблокировать. Не используй основной аккаунт.
- **Telegram видит всё, что в группе**, как и с любым ботом.

---

## Что понадобится

- Два Linux-сервера (команды ниже для Debian/Ubuntu) с доступом по SSH и sudo.
- У A — белый статический IP. У B желательно тоже статический: на него завязан белый список. Если IP у B динамический, смотри раздел «Если у B динамический IP».
- Telegram-бот и его токен ([@BotFather](https://t.me/BotFather)), бот добавлен в группу, Group Privacy выключен.
- Аккаунт MAX и телефон с приложением MAX (для входа по QR).

---

## Шаг 1. Установка (на ОБОИХ серверах)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
sudo useradd --system --create-home --home-dir /opt/max-forwarder --shell /usr/sbin/nologin maxfwd
sudo -u maxfwd git clone https://github.com/AtTheMoro/MAX-Forwarder /opt/max-forwarder/app
sudo -u maxfwd python3 -m venv /opt/max-forwarder/venv
sudo -u maxfwd /opt/max-forwarder/venv/bin/pip install -r /opt/max-forwarder/app/requirements.txt
sudo -u maxfwd cp /opt/max-forwarder/app/config.example.py /opt/max-forwarder/app/config.py
sudo chmod 600 /opt/max-forwarder/app/config.py
```

> Папка `/opt/max-forwarder` закрыта от других пользователей, поэтому обычный админ не может сделать в неё `cd`. Все команды ниже — с полными путями и через `sudo`.

Только на **B** ещё:

```bash
sudo apt install -y ffmpeg
```

## Шаг 2. Общий секрет (на любой машине)

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Запиши результат — он пойдёт в `LINK_SECRET` на обоих серверах. Пересылай его только по защищённому каналу. Лучше всего вставь руками в каждый `config.py` через SSH.

## Шаг 3. TLS-сертификат (на A)

```bash
sudo -u maxfwd openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
  -keyout /opt/max-forwarder/app/link.key -out /opt/max-forwarder/app/link.crt -subj "/CN=max-forwarder"
sudo chmod 600 /opt/max-forwarder/app/link.key
sudo openssl x509 -in /opt/max-forwarder/app/link.crt -noout -fingerprint -sha256
```

Последняя команда печатает отпечаток (`sha256 Fingerprint=…`) — запиши его.

`link.crt` — публичная часть, её надо скопировать на B. `link.key` никуда не копируй.

Скопировать с A на B можно так. На A выложи копию туда, откуда её сможет забрать scp:

```bash
sudo install -m 644 /opt/max-forwarder/app/link.crt /tmp/link.crt
```

Потом на своём компе:

```bash
scp ЮЗЕР@A.A.A.A:/tmp/link.crt .
scp link.crt ЮЗЕР@B.B.B.B:/tmp/link.crt
```

## Шаг 4. Настройка A (за рубежом)

`sudo -u maxfwd nano /opt/max-forwarder/app/config.py`:

```python
ROLE = "tg"
TG_TOKEN = "1234567890:AAF..."      # от @BotFather
TG_CHAT_ID = "-100123456789"         # узнать: написать /chatid в группе (см. шаг 8)
LINK_LISTEN = "0.0.0.0:8443"
LINK_SECRET = "секрет-из-шага-2"
LINK_TLS_CERT = "link.crt"
LINK_TLS_KEY = "link.key"
LINK_ALLOW_IPS = ["B.B.B.B"]
```

Остальное оставь как в шаблоне.

## Шаг 5. Фаервол на A

> [!CAUTION]
> Сначала разреши SSH, иначе после `ufw enable` отрежешь себе доступ. Если SSH у тебя не на 22-м порту — укажи свой.

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit 22/tcp
sudo ufw allow from B.B.B.B to any port 8443 proto tcp
sudo ufw enable
sudo ufw status numbered
```

## Шаг 6. Настройка B (в РФ)

Положи сертификат A на место и сверь отпечаток:

```bash
sudo install -o maxfwd -g maxfwd -m 644 /tmp/link.crt /opt/max-forwarder/app/link.crt
sudo openssl x509 -in /opt/max-forwarder/app/link.crt -noout -fingerprint -sha256
```

Отпечаток **должен совпасть** с тем, что ты записал в шаге 3. Если не совпал — файл подменили или перепутали, дальше не иди.

`sudo -u maxfwd nano /opt/max-forwarder/app/config.py`:

```python
ROLE = "max"
MAX_CHAT_ID = -54321098765432      # из адреса группы на web.max.ru
MAX_AUTH = "qr"
LINK_URL = "wss://A.A.A.A:8443/link"
LINK_SECRET = "секрет-из-шага-2"
LINK_TLS_CA = "link.crt"
```

Фаервол на B: входящие не нужны вообще, кроме SSH.

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit 22/tcp
sudo ufw enable
```

## Шаг 7. Первый вход в MAX (на B, руками)

```bash
sudo -u maxfwd /opt/max-forwarder/venv/bin/python /opt/max-forwarder/app/main.py
```

1. В консоли появится QR-код.
2. Открой приложение MAX: **Настройки → Устройства → Войти по QR-коду**. Отсканируй код.
3. Дождись строки `MAX подключён`.
4. Нажми `Ctrl+C`.

Сессия сохранится в `max_session/`, права на папку выставятся только для владельца. После этого MAX появится в списке устройств аккаунта, и там же его можно отключить.

## Шаг 8. Автозапуск через systemd (на ОБОИХ серверах)

```bash
sudo tee /etc/systemd/system/max-forwarder.service >/dev/null <<'EOF'
[Unit]
Description=MAX-Forwarder
After=network-online.target
Wants=network-online.target

[Service]
User=maxfwd
Group=maxfwd
WorkingDirectory=/opt/max-forwarder/app
ExecStart=/opt/max-forwarder/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/max-forwarder/app
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now max-forwarder
journalctl -u max-forwarder -f
```

Чтобы узнать `TG_CHAT_ID` на A:

1. Временно поставь любое значение, например `"0"`, и запусти сервис.
2. Напиши в группе `/chatid` — бот ответит ID.
3. Впиши его в конфиг и выполни `sudo systemctl restart max-forwarder`.

## Шаг 9. Проверка

**В логах.** На A должно появиться `Жду вторую ноду…`, затем `Подключилась нода с B.B.B.B` и `Связь с max-нодой есть`. На B — `MAX подключён` и `Связь с tg-нодой есть`.

**Что порт закрыт для чужих.** С B (его IP разрешён) без секрета должно быть **401**:

```bash
curl -sk --max-time 5 -o /dev/null -w "%{http_code}\n" https://A.A.A.A:8443/link
```

С любой другой машины та же команда должна выдать `000` по таймауту: фаервол просто молчит.

**Пересылка.** Напиши в обоих чатах текст, фото, войс и кружок.

---

## Обновление

Обновляй **оба** сервера, чтобы версии совпадали:

```bash
sudo -u maxfwd git -C /opt/max-forwarder/app pull
sudo -u maxfwd /opt/max-forwarder/venv/bin/pip install -r /opt/max-forwarder/app/requirements.txt
sudo systemctl restart max-forwarder
```

`config.py`, сертификаты и сессия лежат вне git, поэтому `git pull` их не трогает.

## Если у B динамический IP

Без белого списка IP защита держится на TLS и 256-битном секрете. Подобрать секрет нереально, так что это нормально, просто слоёв защиты меньше. Варианты:

1. На A убери `LINK_ALLOW_IPS`. Правило фаервола замени на `sudo ufw allow 8443/tcp`.
2. Или подними между серверами WireGuard. Тогда слушай только на туннельном адресе (`LINK_LISTEN = "10.0.0.1:8443"`), а порт наружу не открывай вовсе.

## Если связку режут

Повесь A на порт `443`:

1. `LINK_LISTEN = "0.0.0.0:443"`.
2. Добавь в юнит строку `AmbientCapabilities=CAP_NET_BIND_SERVICE`.
3. Поменяй порт в правиле ufw.
4. Поменяй порт в `LINK_URL` на B.

## Ротация секретов и «если сервер взломали»

- **Взломан A:**
  - в @BotFather сделай `/revoke` токена бота;
  - на A сгенерируй новый сертификат (шаг 3) и новый секрет (шаг 2);
  - разнеси их на обе машины.
- **Взломан B:**
  - в приложении MAX: Настройки → Устройства → заверши сеанс форвардера;
  - удали `max_session/`;
  - смени секрет на обеих машинах;
  - войди заново (шаг 7).
- **Плановая смена секрета:** поменяй `LINK_SECRET` на обеих машинах и перезапусти оба сервиса.

## Частые ошибки

| В логе | Что делать |
|---|---|
| `Сервер связи отверг LINK_SECRET` | Секреты на A и B разные |
| `Сервер связи не пускает этот IP` / на A `Отбил подключение с X — нет в LINK_ALLOW_IPS` | Внешний IP B не тот, что в конфиге (NAT, смена IP). Посмотри IP, с которого пришёл B, в логе A и поправь `LINK_ALLOW_IPS` и правило ufw |
| `CERTIFICATE_VERIFY_FAILED` на B | `link.crt` на B не от того ключа (сертификат перегенерировали?). Скопируй заново и сверь отпечаток |
| `Не подключиться к wss://…` без 401/403 | Порт закрыт фаерволом, A не запущен или провайдер режет — см. «Если связку режут» |
| `ffmpeg не найден` на B | `sudo apt install -y ffmpeg` |
| `config.py читают другие пользователи` | `sudo chmod 600 /opt/max-forwarder/app/config.py` |
