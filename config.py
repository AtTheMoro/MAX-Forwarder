# Кто эта машина:
#   "both" — всё в одном процессе (MAX и Telegram доступны с этой машины)
#   "tg"   — только Telegram-половина (сервер за рубежом)
#   "max"  — только MAX-половина (сервер в РФ)
# Можно переопределить при запуске: python3 main.py --role tg
ROLE = "both"

# ---------- MAX (нужно для ROLE = "max" / "both") ----------
MAX_AUTH = ""      # "token" | "qr" | "sms"; пусто = token, если MAX_TOKEN задан, иначе qr
MAX_TOKEN = ""     # токен из web.max.ru (__oneme_auth → token)
MAX_PHONE = ""     # номер аккаунта, нужен только для MAX_AUTH = "sms"
MAX_CHAT_ID = -68920358898409  # ID чата MAX, ИЗ которого пересылать
MAX_SELF_ID = None  # ID в MAX, чьи сообщения не пересылать в TG (опционально)
MAX_SKIP_OWN = False  # True — не пересылать в TG ничего, что пишет сам аккаунт-форвардер
MAX_WORK_DIR = "max_session"  # тут хранится сессия PyMax, удали папку для перелогина
MAX_PROXY = None   # прокси для MAX, например "socks5://user:pass@host:1080"

# ---------- Telegram (нужно для ROLE = "tg" / "both") ----------
TG_TOKEN = ""      # токен бота от @BotFather
TG_CHAT_ID = ""    # ID чата В который пересылать (можно узнать командой /chatid)
TG_PROXY = None    # прокси для Telegram, например "socks5://user:pass@host:1080"

# ---------- Связь между нодами (только для ROLE = "tg" / "max") ----------
# Одна нода слушает (LINK_LISTEN), другая подключается к ней (LINK_URL).
# Обычно слушает зарубежная TG-нода, а MAX-нода из РФ к ней коннектится.
LINK_LISTEN = ""   # например "0.0.0.0:8765"
LINK_URL = ""      # например "wss://1.2.3.4:8765/link"
LINK_SECRET = ""   # одинаковый на обеих нодах, минимум 16 символов
LINK_TLS_CERT = ""  # на слушающей ноде: сертификат и ключ для wss://
LINK_TLS_KEY = ""
LINK_TLS_CA = ""   # на подключающейся ноде: тот же сертификат, чтобы ему доверять
LINK_PROXY = None  # прокси для подключения к другой ноде (http://...)

MUTE = False       # True = только MAX → TG (то же, что --mute)
MAX_FILE_MB = 50   # медиа больше этого не пересылаются (лимит ботов TG — 50 МБ)
LOG_LEVEL = "INFO"
