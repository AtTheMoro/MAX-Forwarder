"""MAX-половина: юзербот на PyMax. Запускается там, где MAX работает (РФ)."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum

import aiohttp
import pymax
import websockets
from pymax import Client, ExtraConfig, File, Photo, Video, VideoNote, Voice, WebClient
from pymax.api.messages.payloads import ReplyLink, SendMessagePayload, SendMessagePayloadMessage
from pymax.api.response import require_payload_model
from pymax.exceptions import ApiError
from pymax.protocol import Opcode
from pymax.protocol.enums import Command

from . import media, pymax_patches, textfmt
from .settings import Settings
from .storage import BoundedSet, read_json, read_names, write_json

log = logging.getLogger("max")

SKIP_ATTACHES = {"CONTROL", "INLINE_KEYBOARD", "WIDGET"}
NETWORK_ERRORS = (
    OSError, EOFError, TimeoutError, ConnectionError,
    aiohttp.ClientError, websockets.exceptions.WebSocketException,
)
RETRY_DELAY = 30


def plain(o):
    """Модель PyMax → такой же dict, как в сыром событии (Enum → строка)."""
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, dict):
        return {k: plain(v) for k, v in o.items()}
    if isinstance(o, list):
        return [plain(v) for v in o]
    return o


class QrPrinter:
    async def show_qr(self, qr_url: str) -> None:
        import qrcode

        print("\nОтсканируй QR в приложении MAX: Настройки → Устройства → Войти по QR-коду")
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_url)
        try:
            qr.print_ascii(invert=True)
        except UnicodeEncodeError:  # консоль не в UTF-8 (бывает под systemd)
            print("(QR не нарисовать в этой консоли — сделай QR из ссылки ниже)")
        print(f"Ссылка из QR: {qr_url}\n", flush=True)


class MaxNames:
    def __init__(self, s: Settings) -> None:
        self.cache_path = s.path("names.json")
        self.cache: dict[str, str] = read_json(self.cache_path, {})
        self.custom = read_names(s.path("custom_names.json"))

    async def get(self, client, uid) -> str:
        if uid is None:
            return "?"
        key = str(uid)
        if key in self.custom:
            return self.custom[key]
        if self.cache.get(key):
            return self.cache[key]
        name = ""
        try:
            user = await asyncio.wait_for(client.get_user(int(uid)), 10)
            if user and user.names:
                n = user.names[0]
                name = n.name or f"{n.first_name or ''} {n.last_name or ''}".strip()
        except Exception as e:
            log.debug("Имя %s не получено: %r", uid, e)
        if name:
            self.cache[key] = name
            try:
                write_json(self.cache_path, self.cache)
            except OSError:
                pass
        return name or key


class MaxSide:
    def __init__(self, s: Settings, link) -> None:
        pymax_patches.apply()
        self.s = s
        self.link = link
        self.chat_id = s.MAX_CHAT_ID
        self.client = self._build_client()
        self.names = MaxNames(s)

        self.from_tg: asyncio.Queue[dict] = asyncio.Queue()
        self.from_max: asyncio.Queue[dict] = asyncio.Queue()
        self.seen = BoundedSet()
        self.own_cids = BoundedSet(2000)
        self.own_ids = BoundedSet(2000)
        self.last_ts = int(time.time() * 1000)
        self.my_id: int | None = None
        self.online = asyncio.Event()
        self.stopping = False

        self.client.on_raw()(self._on_raw)
        self.client.on_start()(self._on_start)
        self.client.on_disconnect()(self._on_disconnect)
        link.set_receiver(self._on_link_event)

    def _build_client(self):
        s = self.s
        work_dir = s.path(s.MAX_WORK_DIR)
        work_dir.mkdir(parents=True, exist_ok=True)
        extra = ExtraConfig(
            token=s.MAX_TOKEN if s.max_auth == "token" else None,
            proxy=s.MAX_PROXY,
            reconnect=True,
            reconnect_delay=5,
            log_level=s.LOG_LEVEL,
        )
        if s.max_auth == "sms":
            return Client(phone=s.MAX_PHONE, work_dir=str(work_dir), extra_config=extra)
        return WebClient(work_dir=str(work_dir), extra_config=extra, qr_provider=QrPrinter())

    # ---------- жизненный цикл ----------

    async def run(self) -> None:
        log.info("PyMax %s, вход: %s, чат %s", pymax.__version__, self.s.max_auth, self.chat_id)
        if not media.has_ffmpeg():
            log.warning("ffmpeg не найден — голосовые и кружки из TG в MAX могут не пройти")
        workers = [
            asyncio.create_task(self._worker(self.from_max, self._handle_max_message)),
            asyncio.create_task(self._worker(self.from_tg, self._handle_link_event)),
        ]
        try:
            while not self.stopping:
                try:
                    await self.client.start()  # внутри сам переподключается
                except NETWORK_ERRORS as e:
                    log.warning("MAX недоступен: %r", e)
                self.online.clear()
                if not self.stopping:
                    log.info("Новая попытка подключиться к MAX через %s с", RETRY_DELAY)
                    await asyncio.sleep(RETRY_DELAY)
        finally:
            for w in workers:
                w.cancel()

    async def close(self) -> None:
        self.stopping = True
        await self.client.close()

    async def _on_start(self, client) -> None:
        me = client.me
        self.my_id = me.contact.id if me else None
        self.online.set()
        log.info("MAX подключён (аккаунт %s)", self.my_id)
        asyncio.create_task(self._fetch_missed())

    def _on_disconnect(self, exc, reconnect, delay) -> None:
        self.online.clear()
        log.warning("MAX отвалился: %r, переподключение: %s", exc, reconnect)

    @staticmethod
    async def _worker(queue: asyncio.Queue, handler) -> None:
        while True:
            item = await queue.get()
            try:
                await handler(item)
            except Exception:
                log.exception("Ошибка обработки")

    # ---------- MAX → TG ----------

    async def _on_raw(self, frame, client) -> None:
        if frame.opcode != Opcode.NOTIF_MESSAGE or frame.cmd != Command.REQUEST:
            return
        payload = frame.payload or {}
        if payload.get("chatId") != self.chat_id:
            return
        msg = payload.get("message")
        if isinstance(msg, dict):
            self.from_max.put_nowait(msg)

    async def _fetch_missed(self) -> None:
        """Догоняем то, что пришло, пока были оффлайн."""
        await asyncio.sleep(1)
        try:
            msgs = await self.client.fetch_history(
                self.chat_id, from_time=self.last_ts, forward=50, backward=0
            )
        except Exception as e:
            log.warning("История не подтянулась: %r", e)
            return
        raw = [plain(m.model_dump(by_alias=True, exclude_none=True)) for m in msgs]
        for m in sorted(raw, key=lambda m: m.get("time") or 0):
            self.from_max.put_nowait(m)

    async def _handle_max_message(self, msg: dict) -> None:
        mid = msg.get("id")
        if not mid or str(mid) in self.seen:
            return
        if msg.get("status") in ("EDITED", "REMOVED"):
            return  # правки и удаления не пересылаем
        self.seen.add(str(mid))
        self.last_ts = max(self.last_ts, msg.get("time") or 0)

        sender = msg.get("sender")
        if msg.get("cid") in self.own_cids or str(mid) in self.own_ids:
            return  # эхо того, что мы сами переслали из TG
        if self.s.MAX_SELF_ID and sender == self.s.MAX_SELF_ID:
            return
        if self.s.MAX_SKIP_OWN and sender == self.my_id:
            return

        event = await self._convert(msg)
        if event:
            await self.link.send(event)

    async def _convert(self, msg: dict) -> dict | None:
        name = await self.names.get(self.client, msg.get("sender"))
        parts = [(msg.get("text") or "", textfmt.from_max(msg.get("elements")))]
        attaches = list(msg.get("attaches") or [])
        reply_to = None

        link = msg.get("link") or {}
        inner = link.get("message") or {}
        if link.get("type") == "REPLY":
            reply_to = inner.get("id") or link.get("messageId")
        elif link.get("type") == "FORWARD":
            src = link.get("chatName") or await self.names.get(self.client, inner.get("sender"))
            fwd = f"↪️ Переслано от {src}"
            parts.append((fwd, [{"type": "italic", "offset": 0, "length": textfmt.u16(fwd)}]))
            parts.append((inner.get("text") or "", textfmt.from_max(inner.get("elements"))))
            attaches += inner.get("attaches") or []

        items, notes = [], []
        for attach in attaches:
            try:
                await self._attach(msg, attach, items, notes)
            except Exception as e:
                log.warning("Вложение %s не обработано: %r", attach.get("_type"), e)
                notes.append(f"[{str(attach.get('_type', '?')).lower()}: не удалось скачать]")

        text, entities = textfmt.join(parts + [(n, []) for n in notes])
        if not text and not items:
            return None
        return {
            "t": "max_msg",
            "max_id": str(msg["id"]),
            "name": name,
            "text": text,
            "entities": entities,
            "reply_to_max": str(reply_to) if reply_to else None,
            "media": items,
        }

    async def _attach(self, msg: dict, a: dict, items: list, notes: list) -> None:
        kind = str(a.get("_type") or a.get("type") or "").upper()
        limit = self.s.max_file_bytes
        msg_id = int(msg["id"])

        async def grab(url):
            try:
                return await media.download(url, limit)
            except media.TooBig as e:
                notes.append(f"[файл больше {self.s.MAX_FILE_MB} МБ, не переслан]")
                log.info("Пропуск большого файла: %s", e)
                return None

        if kind in SKIP_ATTACHES:
            return
        if kind == "PHOTO":
            url = a.get("baseUrl")
            if url and (data := await grab(url)):
                items.append({"kind": "photo", "name": "photo.jpg", "data": data})
        elif kind == "VIDEO":
            info = await self.client.get_video_by_id(self.chat_id, msg_id, int(a["videoId"]))
            round_ = a.get("videoType") == 1
            duration = (a.get("duration") or 0) // 1000 or None
            if info and info.url:
                if data := await grab(info.url):
                    items.append({
                        "kind": "video_note" if round_ else "video",
                        "name": f"{a['videoId']}.mp4",
                        "duration": duration,
                        "data": data,
                    })
            elif info and isinstance(info.external, str):
                notes.append(f"[видео: {info.external}]")
            else:
                notes.append("[видео: MAX не дал ссылку]")
        elif kind == "AUDIO" or (kind == "UNSUPPORTED" and a.get("audioId")):
            url = a.get("url") or (f"https://i.oneme.ru/i?r={a['token']}" if a.get("token") else None)
            data = await grab(url) if url else None
            if data:
                items.append({
                    "kind": "voice",
                    "name": "voice.ogg",
                    "duration": (a.get("duration") or 0) // 1000 or None,
                    "data": data,
                })
            else:
                notes.append("[голосовое: не удалось скачать]")
        elif kind == "FILE":
            name = a.get("name") or "file"
            if (a.get("size") or 0) > limit:
                notes.append(f"[файл {name}: больше {self.s.MAX_FILE_MB} МБ]")
                return
            info = await self.client.get_file_by_id(self.chat_id, msg_id, int(a["fileId"]))
            if info and info.url and (data := await grab(info.url)):
                items.append({"kind": "file", "name": name, "data": data})
            else:
                notes.append(f"[файл {name}: не удалось скачать]")
        elif kind == "STICKER":
            url = a.get("url")
            if url and (data := await grab(url)):
                items.append({"kind": "photo", "name": "sticker.webp", "data": data, "sticker": True})
            else:
                notes.append("[стикер]")
        elif kind == "SHARE":
            notes.append(" ".join(x for x in (a.get("title"), a.get("url")) if x) or "[ссылка]")
        elif kind == "CONTACT":
            notes.append(f"[контакт: {a.get('name') or a.get('firstName') or ''} {a.get('phone') or ''}]".strip())
        elif kind == "POLL":
            poll = a.get("poll") or a
            notes.append(f"[опрос: {poll.get('question') or poll.get('title') or ''}]")
        elif kind == "CALL":
            notes.append("[звонок]")
        else:
            notes.append(f"[вложение {kind or '?'}]")

    # ---------- TG → MAX ----------

    async def _on_link_event(self, event: dict) -> None:
        self.from_tg.put_nowait(event)

    async def _handle_link_event(self, event: dict) -> None:
        if event.get("t") != "tg_msg":
            return
        if self.s.MUTE:
            return
        tg_id = event.get("tg_id")
        try:
            await asyncio.wait_for(self.online.wait(), 600)
        except asyncio.TimeoutError:
            await self.link.send({"t": "max_fail", "tg_id": tg_id, "error": "MAX не подключён"})
            return
        try:
            max_id = await self._post(event)
        except Exception as e:
            log.exception("Не отправилось в MAX")
            await self.link.send({"t": "max_fail", "tg_id": tg_id, "error": str(e)[:300]})
            return
        await self.link.send({"t": "max_ack", "tg_id": tg_id, "max_id": max_id})

    async def _post(self, event: dict) -> str:
        name = event.get("name") or "TG"
        text = event.get("text") or ""
        head = f"[{name}]"
        full = f"{head}: {text}" if text else head
        elements = [{"type": "STRONG", "from": 0, "length": textfmt.u16(head)}]
        elements += textfmt.to_max(textfmt.shift(event.get("entities") or [], textfmt.u16(head) + 2))
        reply_to = int(event["reply_to_max"]) if event.get("reply_to_max") else None

        attachments = [await self._build_attachment(m) for m in event.get("media") or []]
        try:
            msg = await self._send(full, elements, reply_to, attachments)
        except pymax.UploadError as e:
            if not attachments:
                raise
            log.warning("Вложение не загрузилось (%s), шлю только текст", e)
            note = "[вложение не загрузилось]"
            msg = await self._send(f"{full}\n{note}", elements, reply_to, [])
        return str(msg.id)

    async def _build_attachment(self, m: dict):
        kind, data, name = m["kind"], m["data"], m.get("name") or "file"
        duration_ms = int(m["duration"] * 1000) if m.get("duration") else None
        if kind == "photo":
            return Photo(raw=data, name=name)
        if kind == "video":
            return Video(raw=data, name=name if name.endswith(".mp4") else "video.mp4")
        if kind == "video_note":
            return VideoNote(raw=await media.to_max_video_note(data), name="video_note.mp4", duration=duration_ms or 1000)
        if kind == "voice":
            return Voice(raw=await media.to_max_voice(data), name="voice.ogg", duration=duration_ms or 1000)
        return File(raw=data, name=name)

    async def _send(self, text, elements, reply_to, attachments):
        """Свой MSG_SEND вместо client.send_message.

        * текст уходит как есть, без markdown-парсера PyMax (он корёжит _ и *);
        * cid известен до отправки — эхо отсекается без гонок;
        * «attachment ... not.ready» лечим повторами, а не ожиданием
          NOTIF_ATTACH, который для голосовых сервер не шлёт.
        """
        app = self.client._app
        svc = app.api.messages
        last_error: Exception | None = None
        for _ in range(2):  # вторая попытка — с перезаливкой вложений
            attaches = await svc._upload_attachments(attachments)
            for delay in (0, 2, 3, 5, 8, 12):
                await asyncio.sleep(delay)
                cid = svc._next_cid()
                self.own_cids.add(cid)
                frame = SendMessagePayload(
                    chat_id=self.chat_id,
                    message=SendMessagePayloadMessage(
                        text=text or None,
                        cid=cid,
                        elements=elements,
                        attaches=attaches,
                        link=ReplyLink(message_id=reply_to) if reply_to else None,
                    ),
                    notify=True,
                )
                try:
                    response = await app.invoke(Opcode.MSG_SEND, frame.to_payload())
                except ApiError as e:
                    last_error = e
                    if "not.ready" in str(e.error or ""):
                        continue
                    if reply_to:  # исходник для ответа мог быть удалён
                        log.warning("MAX отверг ответ (%s), шлю без reply", e.error)
                        reply_to = None
                        continue
                    raise
                message = require_payload_model(response, pymax.Message)
                self.own_ids.add(str(message.id))
                return message
            log.warning("Вложение так и не стало готовым, перезаливаю")
        raise pymax.UploadError(f"MAX не принял вложение: {last_error}")
