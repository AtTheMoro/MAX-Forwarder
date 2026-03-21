# MAX-Forwarder
> [!WARNING]  
> Может блокировать аккаунт

Форвардит сообщения из группы [Max](https://max.ru) в Telegram и обратно. Работает как userbot через WebSocket без официального бота API.

> Вдохновлен идеей от [Sharkow1743/SferumTransferBot](https://github.com/Sharkow1743/SferumTransferBot) 

## Что умеет

- Макс → Telegram: текст, фото, видео, файлы, ответы на сообщения
- Telegram → Макс: текст, фото. 
> <small> ответы на сообщения из MAX в Telegram не видно </small>
- Кастомные имена для пользователей Макса и Telegram
- Режим `--mute` (только Макс → TG)

## Требования

- Python 3.11+
- Аккаунт в Max
- Telegram бот токен (через @BotFather)

## Установка

### Linux / macOS

```bash
git clone https://github.com/AtTheMoro/MAX-Forwarder
cd Max-Forwarder
pip install -r requirements.txt
```

### Windows

```bash
git clone https://github.com/AtTheMoro/MAX-Forwarder
cd Max-Forwarder
pip install -r requirements.txt
```

## Настройка

### 1. Получить MAX токен (Описано на примере Firefox-Based браузеров)

1. Зайдите на [web.max.ru](https://web.max.ru) и авторизуйтесь
2. Откройте DevTools: `F12`
3. Перейдите в **Хранилище** (Storage) → **Локальное хранилище** → `https://web.max.ru`
4. Найдите ключ `__oneme_auth` и скопируйте значение поля `token` из JSON

### 2. Получить Telegram токен

1. Напишите [@BotFather](https://t.me/BotFather) в Telegram
2. `/newbot` → придумайте имя → придумайте юзернейм → получите токен
3. Добавьте бота в нужную TG группу
4. Отключите Privacy Mode: BotFather → `/mybots` → Bot Settings → Group Privacy → Turn off

### 3. Узнать chat ID группы Макс

Откройте нужную группу на web.max.ru — в адресной строке будет например `https://web.max.ru/-54321098765432`, число и есть ID.

### 4. Узнать chat ID Telegram группы

Напишите @userinfobot и скиньте группу, получите ID.
### Или
Напишите боту `/start` в группе, затем:
```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```
В ответе найдите `"chat":{"id":...}`.

### 5. Создать config.py

```python
MAX_TOKEN = "твой_токен_из___oneme_auth"
MAX_PHONE = "+7XXXXXXXXXX"  # номер телефона аккаунта Max
MAX_CHAT_ID =  # ID группы в Max
MAX_SELF_ID = 211565775  # viewerId из __oneme_auth (опционально)

TG_TOKEN = "1234567890:AAF..."
TG_CHAT_ID = "-123232323"  # ID группы в Telegram
```

## Кастомные имена

### Пользователи Макса (`custom_names.json`)

Переопределяет автоматически определённые имена:

```json
{
    "211565325": "Никита Моров"
}
```

ID берётся из `names.json` который заполняется автоматически при работе.

### Пользователи Telegram (`tg_names.json`)

Имя которое будет показываться в Максе когда человек пишет из TG:

```json
{
    "_comment": "TG user id → имя в MAX",
    "7213661210": "Никита"
}
```

TG user ID можно узнать через [@userinfobot](https://t.me/userinfobot).

## Запуск

```bash
# Обычный режим (двусторонний)
python3 main.py

# Только Макс → Telegram
python3 main.py --mute
```

## Автозапуск (Linux systemd)

```ini
# ~/.config/systemd/user/maxforwarder.service
[Unit]
Description=MaxForwarder
After=network.target

[Service]
WorkingDirectory= /home/user(Ваш пользователь)/MAX-Forwarder
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now maxforwarder
```


**Реконнекты каждые ~30 сек** — нормальное поведение, сервер MAX закрывает соединение после каждого сообщения. Сообщения не теряются благодаря `fetch_history` при переподключении.
