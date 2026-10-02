"""Канал между половинками форвардера: TG-нода (за рубежом) ↔ MAX-нода (в РФ).

``LocalLink`` — обе половинки в одном процессе (ROLE = "both").
``WsLink`` — WebSocket между двумя машинами. Одна сторона слушает
(LINK_LISTEN), вторая подключается (LINK_URL); кто есть кто — неважно.

Событие — dict, где медиа лежат как bytes. По сети оно упаковывается в
бинарный кадр, большие кадры режутся на куски (чтобы ping/ack не ждали
за 50-мегабайтным видео). Каждое событие получает seq и висит в очереди,
пока вторая сторона не подтвердит приём — при обрыве переотправляется.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import ssl
import struct
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

log = logging.getLogger("link")

Receiver = Callable[[dict], Awaitable[None]]

PATH = "/link"
CHUNK = 256 * 1024
MAX_MSG = CHUNK + 1024
MAX_EVENT = 300 * 1024 * 1024
MAX_PENDING_BYTES = 512 * 1024 * 1024

_FULL = 0x01
_PART = 0x02  # [id:8][idx:4][total:4][data]


def pack(event: dict) -> bytes:
    blobs: list[bytes] = []

    def strip(o):
        if isinstance(o, (bytes, bytearray, memoryview)):
            blobs.append(bytes(o))
            return {"$blob": len(blobs) - 1}
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [strip(v) for v in o]
        return o

    header = json.dumps(
        {"ev": strip(event), "blobs": [len(b) for b in blobs]}, ensure_ascii=False
    ).encode()
    return struct.pack(">I", len(header)) + header + b"".join(blobs)


def unpack(frame: bytes) -> dict:
    (hlen,) = struct.unpack_from(">I", frame)
    header = json.loads(frame[4 : 4 + hlen])
    blobs, pos = [], 4 + hlen
    for n in header["blobs"]:
        blobs.append(frame[pos : pos + n])
        pos += n

    def restore(o):
        if isinstance(o, dict):
            if o.keys() == {"$blob"}:
                return blobs[o["$blob"]]
            return {k: restore(v) for k, v in o.items()}
        if isinstance(o, list):
            return [restore(v) for v in o]
        return o

    return restore(header["ev"])


class LocalLink:
    """Обе половинки в одном процессе — просто передаём dict соседу."""

    def __init__(self) -> None:
        self.peer: LocalLink | None = None
        self.receiver: Receiver | None = None

    @classmethod
    def pair(cls) -> tuple[LocalLink, LocalLink]:
        a, b = cls(), cls()
        a.peer, b.peer = b, a
        return a, b

    def set_receiver(self, receiver: Receiver) -> None:
        self.receiver = receiver

    async def send(self, event: dict) -> None:
        assert self.peer and self.peer.receiver
        await self.peer.receiver(event)

    async def run(self) -> None:
        await asyncio.Event().wait()


class WsLink:
    def __init__(
        self,
        role: str,
        secret: str,
        listen: str = "",
        url: str = "",
        tls_cert: str = "",
        tls_key: str = "",
        tls_ca: str = "",
        proxy: str | None = None,
    ) -> None:
        self.role = role
        self.secret = secret
        self.listen = listen
        self.url = url
        self.tls_cert, self.tls_key, self.tls_ca = tls_cert, tls_key, tls_ca
        self.proxy = proxy
        self.receiver: Receiver | None = None

        self.instance = secrets.token_hex(8)
        self._seq = 0
        self._pending: OrderedDict[int, bytes] = OrderedDict()
        self._pending_bytes = 0
        self._ws: web.WebSocketResponse | aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._peer_instance: str | None = None
        self._seen: deque[int] = deque(maxlen=20000)
        self._seen_set: set[int] = set()
        self._parts: dict[int, list[bytes]] = {}

    def set_receiver(self, receiver: Receiver) -> None:
        self.receiver = receiver

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ---------- отправка ----------

    async def send(self, event: dict) -> None:
        self._seq += 1
        frame = pack({**event, "_seq": self._seq})
        if len(frame) > MAX_EVENT:
            log.error("Событие %s байт — слишком большое, выкидываю", len(frame))
            return
        self._pending[self._seq] = frame
        self._pending_bytes += len(frame)
        while self._pending_bytes > MAX_PENDING_BYTES and len(self._pending) > 1:
            seq, old = self._pending.popitem(last=False)
            self._pending_bytes -= len(old)
            log.warning("Очередь на отправку переполнена, выкинул событие #%s", seq)

        async with self._send_lock:
            ws = self._ws
            if ws is None or ws.closed:
                log.info("Связи нет, событие #%s ждёт в очереди (%s шт.)", self._seq, len(self._pending))
                return
            try:
                await self._write(ws, frame)
            except Exception as e:  # уйдёт повторно после реконнекта
                log.warning("Не отправилось, повторю после переподключения: %r", e)

    @staticmethod
    async def _write(ws, frame: bytes) -> None:
        if len(frame) <= CHUNK:
            await ws.send_bytes(bytes([_FULL]) + frame)
            return
        msg_id = secrets.randbits(63)
        total = (len(frame) + CHUNK - 1) // CHUNK
        for idx in range(total):
            head = bytes([_PART]) + struct.pack(">QII", msg_id, idx, total)
            await ws.send_bytes(head + frame[idx * CHUNK : (idx + 1) * CHUNK])

    def _acked(self, seq: int) -> None:
        frame = self._pending.pop(seq, None)
        if frame is not None:
            self._pending_bytes -= len(frame)

    # ---------- приём ----------

    def _assemble(self, data: bytes) -> bytes | None:
        if data[0] == _FULL:
            return data[1:]
        msg_id, idx, total = struct.unpack_from(">QII", data, 1)
        parts = self._parts.setdefault(msg_id, [])
        if idx != len(parts):  # куски идут строго по порядку в одном соединении
            self._parts.pop(msg_id, None)
            raise ValueError("Битая последовательность кусков")
        parts.append(data[17:])
        if len(parts) < total:
            return None
        return b"".join(self._parts.pop(msg_id))

    async def _session(self, ws) -> None:
        await ws.send_str(json.dumps({"t": "hello", "role": self.role, "instance": self.instance}))
        hello_msg = await ws.receive(timeout=30)
        if hello_msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError("Вторая сторона не прислала hello")
        hello = json.loads(hello_msg.data)
        peer_role = hello.get("role")
        if hello.get("t") != "hello" or peer_role == self.role:
            log.error("Вторая нода тоже с ролью %r — на одной должна быть tg, на другой max", peer_role)
            await ws.close()
            return
        if hello.get("instance") != self._peer_instance:
            self._peer_instance = hello.get("instance")
            self._seen.clear()
            self._seen_set.clear()
        self._parts.clear()

        async with self._send_lock:
            old, self._ws = self._ws, ws
            log.info("Связь с %s-нодой есть. В очереди на отправку: %s", peer_role, len(self._pending))
            try:
                for frame in list(self._pending.values()):
                    await self._write(ws, frame)
            except Exception:
                if self._ws is ws:
                    self._ws = None
                raise
        if old is not None and old is not ws and not old.closed:
            await old.close()

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frame = self._assemble(msg.data)
                    if frame is not None:
                        await self._on_frame(ws, frame)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    ctl = json.loads(msg.data)
                    if ctl.get("t") == "ack":
                        self._acked(int(ctl["seq"]))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise ConnectionError(ws.exception())
        finally:
            if self._ws is ws:
                self._ws = None
            log.warning("Связь с %s-нодой потеряна", peer_role)

    async def _on_frame(self, ws, frame: bytes) -> None:
        event = unpack(frame)
        seq = event.pop("_seq", None)
        if seq is not None and seq in self._seen_set:
            await ws.send_str(json.dumps({"t": "ack", "seq": seq}))
            return
        try:
            if self.receiver:
                await self.receiver(event)
        except Exception:
            log.exception("Обработчик события упал")
        if seq is not None:
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(seq)
            self._seen_set.add(seq)
            await ws.send_str(json.dumps({"t": "ack", "seq": seq}))

    # ---------- сервер / клиент ----------

    async def run(self) -> None:
        if self.listen:
            await self._run_server()
        else:
            await self._run_client()

    async def _run_server(self) -> None:
        host, _, port = self.listen.rpartition(":")
        ssl_ctx = None
        if self.tls_cert:
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(self.tls_cert, self.tls_key)

        app = web.Application()
        app.router.add_get(PATH, self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host or "0.0.0.0", int(port), ssl_context=ssl_ctx)
        await site.start()
        scheme = "wss" if ssl_ctx else "ws"
        log.info("Жду вторую ноду на %s://%s:%s%s", scheme, host or "0.0.0.0", port, PATH)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {self.secret}".encode()):
            log.warning("Отбил подключение с неверным секретом от %s", request.remote)
            return web.Response(status=401)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MSG, compress=False)
        await ws.prepare(request)
        log.info("Подключилась нода с %s", request.remote)
        try:
            await self._session(ws)
        except Exception as e:
            log.warning("Сессия связи упала: %r", e)
        return ws

    async def _run_client(self) -> None:
        ssl_arg: ssl.SSLContext | bool = True
        if self.tls_ca:
            # Свой самоподписанный сертификат: доверяем только ему, имя не проверяем
            ssl_arg = ssl.create_default_context(cafile=self.tls_ca)
            ssl_arg.check_hostname = False
        headers = {"Authorization": f"Bearer {self.secret}"}
        delay = 1.0
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(
                        self.url,
                        headers=headers,
                        heartbeat=30,
                        max_msg_size=MAX_MSG,
                        ssl=ssl_arg,
                        proxy=self.proxy,
                    ) as ws:
                        delay = 1.0
                        await self._session(ws)
                except aiohttp.WSServerHandshakeError as e:
                    if e.status == 401:
                        log.error("Сервер связи отверг LINK_SECRET — проверь, что он одинаковый")
                    else:
                        log.warning("Не подключиться к %s: %s", self.url, e)
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ConnectionError) as e:
                    log.warning("Не подключиться к %s: %r", self.url, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
