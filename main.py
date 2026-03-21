import asyncio, time, json, sys, signal
from pathlib import Path
import httpx
from pymax import MaxClient
from pymax.files import Photo
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message as TgMessage
from aiogram.filters import Command
from config import MAX_TOKEN, MAX_PHONE, MAX_CHAT_ID, TG_TOKEN, TG_CHAT_ID, MAX_SELF_ID

from pymax.static import constant
constant.DEFAULT_PING_INTERVAL = 3.0

MUTE = "--mute" in sys.argv
if MUTE:
    print("🔇 Режим --mute: TG → MAX отключён")

client = MaxClient(MAX_PHONE, token=MAX_TOKEN, work_dir="/tmp/max_cache", reconnect=True)
bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

last_ts = int(time.time() * 1000)
seen_ids = set()

CACHE_FILE = Path(__file__).parent / "names.json"
CUSTOM_FILE = Path(__file__).parent / "custom_names.json"
TG_NAMES_FILE = Path(__file__).parent / "tg_names.json"
MAP_FILE = Path(__file__).parent / "msg_map.json"

name_cache: dict = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
custom_names: dict = json.loads(CUSTOM_FILE.read_text()) if CUSTOM_FILE.exists() else {}
tg_names: dict = {k: v for k, v in json.loads(TG_NAMES_FILE.read_text()).items() if not k.startswith("_")} if TG_NAMES_FILE.exists() else {}

_map = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {"m2t": {}, "t2m": {}}
max_to_tg: dict[str, int] = _map["m2t"]
tg_to_max: dict[int, str] = {int(k): v for k, v in _map["t2m"].items()}

name_queue: asyncio.Queue = None
stop_event = asyncio.Event()

def save_cache():
    CACHE_FILE.write_text(json.dumps(name_cache, ensure_ascii=False))

def save_map():
    MAP_FILE.write_text(json.dumps({"m2t": max_to_tg, "t2m": {str(k): v for k, v in tg_to_max.items()}}, ensure_ascii=False))

async def resolve_names():
    while not stop_event.is_set():
        try:
            sender_id = await asyncio.wait_for(name_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if str(sender_id) in name_cache:
            continue
        for _ in range(5):
            if stop_event.is_set():
                return
            if not client.is_connected:
                await asyncio.sleep(1)
                continue
            try:
                user = await client.get_user(sender_id)
                if user:
                    names = getattr(user, 'names', []) or []
                    if names:
                        n = names[0]
                        fn = getattr(n, 'first_name', '') or ''
                        ln = getattr(n, 'last_name', '') or ''
                        name_cache[str(sender_id)] = f"{fn} {ln}".strip()
                    else:
                        name_cache[str(sender_id)] = str(sender_id)
                    save_cache()
                break
            except Exception:
                await asyncio.sleep(2)

def get_name(sender_id: int) -> str:
    key = str(sender_id)
    if key in custom_names:
        return custom_names[key]
    if key not in name_cache:
        try:
            name_queue.put_nowait(sender_id)
        except Exception:
            pass
    return name_cache.get(key, str(sender_id))

def tg_display_name(msg: TgMessage) -> str:
    u = msg.from_user

    if str(u.id) in tg_names:
        return tg_names[str(u.id)]
    return u.full_name or u.username or "TG"

async def tg_text(name, text, reply_to_tg_id=None) -> int | None:
    payload = {"chat_id": TG_CHAT_ID, "text": f"<b>{name}</b>\n{text}", "parse_mode": "HTML"}
    if reply_to_tg_id:
        payload["reply_to_message_id"] = reply_to_tg_id
    async with httpx.AsyncClient() as http:
        r = await http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload)
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    return None

async def tg_photo(name, url, caption="", reply_to_tg_id=None) -> int | None:
    payload = {"chat_id": TG_CHAT_ID, "photo": url,
               "caption": f"<b>{name}</b>\n{caption}", "parse_mode": "HTML"}
    if reply_to_tg_id:
        payload["reply_to_message_id"] = reply_to_tg_id
    async with httpx.AsyncClient() as http:
        r = await http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", json=payload)
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        img = await http.get(url)
        r2 = await http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT_ID, "caption": f"<b>{name}</b>\n{caption}",
                  "parse_mode": "HTML", **({"reply_to_message_id": reply_to_tg_id} if reply_to_tg_id else {})},
            files={"photo": img.content})
        d2 = r2.json()
        if d2.get("ok"):
            return d2["result"]["message_id"]
    return None

async def send_to_tg(msg_data: dict):
    sender_id = msg_data.get('sender')
    name = get_name(sender_id) if sender_id else '?'
    text = msg_data.get('text', '') or ''
    attaches = msg_data.get('attaches', []) or []
    max_id = str(msg_data.get('id') or msg_data.get('cid') or '')

    reply_to_tg_id = None
    reply_info = (msg_data.get('reply') or msg_data.get('replyTo') or
                  msg_data.get('quotedMessage') or msg_data.get('replyMessage'))
    if reply_info:
        parent_max_id = str(reply_info.get('id') or reply_info.get('messageId') or
                            reply_info.get('msgId') or '')
        reply_to_tg_id = max_to_tg.get(parent_max_id)

    tg_id = None
    for attach in attaches:
        atype = str(attach.get('_type') or attach.get('type', '')).upper()
        if atype == 'PHOTO':
            url = attach.get('baseUrl', '')
            if url:
                tg_id = await tg_photo(name, url, text, reply_to_tg_id)
                break
        elif atype in ('VIDEO', 'FILE'):
            tg_id = await tg_text(name, f"[{atype}] {text}", reply_to_tg_id)
            break
    else:
        if text:
            tg_id = await tg_text(name, text, reply_to_tg_id)

    if tg_id and max_id:
        max_to_tg[max_id] = tg_id
        tg_to_max[tg_id] = max_id
        save_map()

async def process_history_msg(msg):
    global last_ts
    msg_id = getattr(msg, 'id', None)
    if not msg_id or msg_id in seen_ids:
        return
    seen_ids.add(msg_id)
    ts = getattr(msg, 'time', 0) or 0
    if ts > last_ts:
        last_ts = ts
    await send_to_tg({
        'id': msg_id,
        'sender': getattr(msg, 'sender', None),
        'text': getattr(msg, 'text', '') or '',
        'attaches': getattr(msg, 'attaches', []) or [],
    })

async def fetch_missed():
    await asyncio.sleep(1)
    if not client.is_connected:
        return
    try:
        msgs = await client.fetch_history(MAX_CHAT_ID, from_time=last_ts, forward=50, backward=0)
        if msgs:
            for msg in reversed(msgs):
                await process_history_msg(msg)
    except Exception:
        pass

async def raw(data):
    if data.get('opcode') != 128:
        return
    payload = data.get('payload', {})
    if payload.get('chatId') != MAX_CHAT_ID:
        return
    msg = payload.get('message', {})
    msg_id = msg.get('id')
    if not msg_id or msg_id in seen_ids:
        return
    seen_ids.add(msg_id)
    global last_ts
    ts = msg.get('time', 0) or 0
    if ts > last_ts:
        last_ts = ts
    if msg.get("sender") == MAX_SELF_ID:
        return
    await send_to_tg(msg)

async def forward_to_max(name, text, reply_to_max_id=None):
    await client.send_message(
        text=f"[{name}]: {text}",
        chat_id=MAX_CHAT_ID,
        reply_to=int(reply_to_max_id) if reply_to_max_id else None
    )

@dp.message(Command("send"))
async def tg_send_text(msg: TgMessage):
    if MUTE:
        await msg.reply("🔇 Бот в режиме mute")
        return
    text = msg.text.removeprefix("/send").strip()
    if not text:
        await msg.reply("Использование: /send текст (Не работает)")
        return
    if not client.is_connected:
        await msg.reply("❌ Макс не подключён")
        return
    name = tg_display_name(msg)
    reply_to_max_id = tg_to_max.get(msg.reply_to_message.message_id) if msg.reply_to_message else None
    await forward_to_max(name, text, reply_to_max_id)
    await msg.reply("✅")

@dp.message(F.photo)
async def tg_send_photo(msg: TgMessage):
    if MUTE:
        return
    if not client.is_connected:
        await msg.reply("❌ Макс не подключён")
        return
    photo = msg.photo[-1]
    file = await bot.get_file(photo.file_id)
    async with httpx.AsyncClient() as http:
        r = await http.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file.file_path}")
        img_bytes = r.content
    name = tg_display_name(msg)
    caption = msg.caption or ""
    reply_to_max_id = tg_to_max.get(msg.reply_to_message.message_id) if msg.reply_to_message else None
    await client.send_message(
        text=f"[{name}]: {caption}",
        chat_id=MAX_CHAT_ID,
        attachment=Photo(data=img_bytes),
        reply_to=int(reply_to_max_id) if reply_to_max_id else None
    )
    await msg.reply("✅")

@dp.message(F.text & ~F.text.startswith("/"))
async def tg_send_plain(msg: TgMessage):
    if MUTE:
        return
    if msg.from_user.is_bot:
        return
    if str(msg.chat.id) != str(TG_CHAT_ID):
        return
    if not client.is_connected:
        return
    name = tg_display_name(msg)
    reply_to_max_id = tg_to_max.get(msg.reply_to_message.message_id) if msg.reply_to_message else None
    await forward_to_max(name, msg.text, reply_to_max_id)

async def on_start():
    asyncio.create_task(fetch_missed())

client._on_raw_receive_handlers.append(raw)
client._on_start_handler = on_start

async def shutdown():
    print("\nОстанавливаемся...")
    stop_event.set()
    await dp.stop_polling()
    await bot.session.close()
    await client.close()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

async def main():
    global name_queue
    name_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
    print("Бот запущен, слушаю Max и Telegram")
    asyncio.create_task(resolve_names())
    asyncio.create_task(dp.start_polling(bot, allowed_updates=["message"]))
    await client.start()

asyncio.run(main())
