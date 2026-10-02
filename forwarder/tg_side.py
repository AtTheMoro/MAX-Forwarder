"""TG-половина: бот на aiogram. Запускается там, где Telegram работает (за рубежом)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, Message, MessageEntity, ReplyParameters

from . import textfmt
from .settings import Settings
from .storage import MsgMap, read_names

log = logging.getLogger("tg")

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # больше Bot API скачать не даёт
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TgSide:
    def __init__(self, s: Settings, link) -> None:
        self.s = s
        self.link = link
        self.chat_id = int(s.TG_CHAT_ID)
        session = AiohttpSession(proxy=s.TG_PROXY) if s.TG_PROXY else None
        self.bot = Bot(s.TG_TOKEN, session=session)
        self.dp = Dispatcher()
        self.dp.message.register(self._on_tg_message)
        self.names = read_names(s.path("tg_names.json"))
        self.map = MsgMap(s.path("msg_map.json"))
        self.from_tg: asyncio.Queue[Message] = asyncio.Queue()
        self.from_max: asyncio.Queue[dict] = asyncio.Queue()
        link.set_receiver(self._on_link_event)

    async def run(self) -> None:
        while True:
            try:
                me = await self.bot.get_me()
                break
            except TelegramNetworkError as e:
                log.warning("Telegram недоступен (%s), повтор через 15 с", e)
                await asyncio.sleep(15)
        log.info("Telegram-бот @%s, чат %s", me.username, self.chat_id)
        workers = [
            asyncio.create_task(self._worker(self.from_tg, self._forward_tg_message)),
            asyncio.create_task(self._worker(self.from_max, self._handle_link_event)),
        ]
        try:
            await self.dp.start_polling(self.bot, handle_signals=False, allowed_updates=["message"])
        finally:
            for w in workers:
                w.cancel()

    async def close(self) -> None:
        try:
            await self.dp.stop_polling()
        except RuntimeError:
            pass
        await self.bot.session.close()

    @staticmethod
    async def _worker(queue: asyncio.Queue, handler) -> None:
        while True:
            item = await queue.get()
            try:
                await handler(item)
            except Exception:
                log.exception("Ошибка обработки")

    async def _call(self, method, *args, **kwargs):
        """Вызов Bot API с повтором на флуд-лимит и сетевые сбои."""
        for attempt in range(5):
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramNetworkError as e:
                if attempt == 4:
                    raise
                log.warning("Сеть до Telegram моргнула: %s", e)
                await asyncio.sleep(2 * (attempt + 1))
        return await method(*args, **kwargs)

    # ---------- TG → MAX ----------

    async def _on_tg_message(self, msg: Message) -> None:
        text = msg.text or ""
        if text.startswith("/chatid"):
            await msg.reply(f"chat id: <code>{msg.chat.id}</code>", parse_mode="HTML")
            return
        if msg.chat.id != self.chat_id or text.startswith("/"):
            return
        if msg.from_user and msg.from_user.is_bot:
            return
        if self.s.MUTE:
            return
        self.from_tg.put_nowait(msg)

    def _tg_name(self, msg: Message) -> str:
        if msg.sender_chat:
            return msg.sender_chat.title or "Аноним"
        u = msg.from_user
        if u is None:
            return "Аноним"
        return self.names.get(str(u.id)) or u.full_name or u.username or "TG"

    async def _forward_tg_message(self, msg: Message) -> None:
        parts = []
        if msg.forward_origin:
            origin = msg.forward_origin
            who = (
                getattr(getattr(origin, "sender_user", None), "full_name", None)
                or getattr(origin, "sender_user_name", None)
                or getattr(getattr(origin, "chat", None), "title", None)
                or getattr(getattr(origin, "sender_chat", None), "title", None)
                or "?"
            )
            parts.append((f"↪️ Переслано от {who}", []))
        parts.append((msg.text or msg.caption or "", textfmt.from_tg(msg.entities or msg.caption_entities)))

        item, notes = None, []
        if msg.photo:
            item = ("photo", msg.photo[-1], "photo.jpg", None)
        elif msg.video:
            item = ("video", msg.video, msg.video.file_name or "video.mp4", msg.video.duration)
        elif msg.animation:
            item = ("video", msg.animation, msg.animation.file_name or "animation.mp4", msg.animation.duration)
        elif msg.video_note:
            item = ("video_note", msg.video_note, "video_note.mp4", msg.video_note.duration)
        elif msg.voice:
            item = ("voice", msg.voice, "voice.ogg", msg.voice.duration)
        elif msg.audio:
            item = ("file", msg.audio, msg.audio.file_name or "audio.mp3", msg.audio.duration)
        elif msg.document:
            item = ("file", msg.document, msg.document.file_name or "file", None)
        elif msg.sticker:
            if msg.sticker.is_animated or msg.sticker.is_video:
                notes.append(f"[стикер {msg.sticker.emoji or ''}]".replace(" ]", "]"))
            else:
                item = ("photo", msg.sticker, "sticker.webp", None)
        elif msg.location:
            notes.append(f"📍 https://yandex.ru/maps/?pt={msg.location.longitude},{msg.location.latitude}&z=16")
        elif msg.contact:
            c = msg.contact
            notes.append(f"[контакт: {c.first_name} {c.last_name or ''} {c.phone_number}]")
        elif msg.poll:
            options = "\n".join(f"• {o.text}" for o in msg.poll.options)
            notes.append(f"[опрос: {msg.poll.question}]\n{options}")
        elif msg.dice:
            notes.append(f"{msg.dice.emoji} {msg.dice.value}")

        media = []
        if item:
            kind, obj, name, duration = item
            if (obj.file_size or 0) > TG_DOWNLOAD_LIMIT:
                notes.append("[файл больше 20 МБ — бот Telegram такие не скачивает]")
            else:
                buf = await self._call(self.bot.download, obj.file_id, timeout=120)
                media.append({"kind": kind, "name": name, "duration": duration, "data": buf.read()})

        text, entities = textfmt.join(parts + [(n, []) for n in notes])
        if not text and not media:
            return
        reply_to_max = self.map.max(msg.reply_to_message.message_id) if msg.reply_to_message else None
        await self.link.send({
            "t": "tg_msg",
            "tg_id": msg.message_id,
            "name": self._tg_name(msg),
            "text": text,
            "entities": entities,
            "reply_to_max": reply_to_max,
            "media": media,
        })

    # ---------- MAX → TG ----------

    async def _on_link_event(self, event: dict) -> None:
        self.from_max.put_nowait(event)

    async def _handle_link_event(self, event: dict) -> None:
        kind = event.get("t")
        if kind == "max_msg":
            await self._post(event)
        elif kind == "max_ack":
            self.map.add(event["max_id"], [event["tg_id"]])
        elif kind == "max_fail":
            await self._call(
                self.bot.send_message,
                self.chat_id,
                f"⚠️ В MAX не ушло: {event.get('error') or '?'}",
                reply_parameters=self._reply(event.get("tg_id")),
            )

    def _reply(self, message_id) -> ReplyParameters | None:
        if not message_id:
            return None
        return ReplyParameters(message_id=int(message_id), allow_sending_without_reply=True)

    async def _post(self, event: dict) -> None:
        name = event.get("name") or "?"
        text = event.get("text") or ""
        body = f"{name}\n{text}" if text else name
        entities = [{"type": "bold", "offset": 0, "length": textfmt.u16(name)}]
        entities += textfmt.shift(event.get("entities") or [], textfmt.u16(name) + 1)
        tg_entities = [MessageEntity(**e) for e in entities]
        reply = self._reply(self.map.tg(event.get("reply_to_max")))
        items = event.get("media") or []
        sent: list[int] = []

        caption_ok = textfmt.u16(body) <= CAPTION_LIMIT and not any(
            m["kind"] == "video_note" or m.get("sticker") for m in items
        )
        if not items or not caption_ok:
            for chunk_text, chunk_ents in self._split(body, tg_entities):
                m = await self._call(
                    self.bot.send_message, self.chat_id, chunk_text,
                    entities=chunk_ents, reply_parameters=reply,
                )
                sent.append(m.message_id)
                reply = None

        for i, item in enumerate(items):
            with_caption = caption_ok and i == 0
            m = await self._send_media(
                item,
                body if with_caption else None,
                tg_entities if with_caption else None,
                reply if i == 0 else None,
            )
            if m:
                sent.append(m.message_id)

        self.map.add(event.get("max_id"), sent)

    @staticmethod
    def _split(text: str, entities: list[MessageEntity]):
        if textfmt.u16(text) <= TEXT_LIMIT:
            return [(text, entities)]
        # длинный текст режем без форматирования
        return [(text[i : i + 4000], None) for i in range(0, len(text), 4000)]

    async def _send_media(self, item: dict, caption, entities, reply):
        kind = item["kind"]
        file = BufferedInputFile(item["data"], filename=item.get("name") or "file")
        common = {"reply_parameters": reply}
        cap = {"caption": caption, "caption_entities": entities}
        b, chat = self.bot, self.chat_id
        attempts = {
            "photo": [(b.send_photo, {**cap}), (b.send_document, {**cap})],
            "video": [(b.send_video, {**cap, "duration": item.get("duration"), "supports_streaming": True}),
                      (b.send_document, {**cap})],
            "video_note": [(b.send_video_note, {"duration": item.get("duration")}),
                           (b.send_video, {"duration": item.get("duration")})],
            "voice": [(b.send_voice, {**cap, "duration": item.get("duration")}),
                      (b.send_audio, {**cap}), (b.send_document, {**cap})],
            "file": [(b.send_document, {**cap})],
        }.get(kind, [(b.send_document, {**cap})])

        for method, extra in attempts:
            try:
                return await self._call(method, chat, file, **common, **extra)
            except TelegramBadRequest as e:
                log.warning("%s не прошёл (%s), пробую иначе", method.__name__, e.message)
        return None
