"""Заплатки для maxapi-python (PyMax) 2.4.x.

В 2.4.1 часть загрузок медиа сломана со стороны библиотеки, а сервер MAX
за это время поменял поведение. Ниже — точечные замены методов PyMax.

Почти все находки взяты из MAX2TG-Bridge (MIT, https://github.com/miharoot/MAX2TG-Bridge),
где их отловили на живом сервере, и из issue MaxApiTeam/PyMax#102, #103:

* голосовое: PyMax шлёт сырой body с Content-Range, octet-stream и битым
  User-Agent ``OKMessages/None``, а в MSG_SEND ссылается на «видео»-токен.
  Сервер принимает только multipart-форму и аттач ``{_type: AUDIO, audioId}``;
* фото: сервер перестал класть ``photoIds`` в upload URL, PyMax падает с KeyError;
* NOTIF_ATTACH для голосовых опознаётся как VIDEO_READY (не тот waiter).
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import aiohttp
import pymax

log = logging.getLogger("pymax_patches")

_applied = False


class VoiceRejected(pymax.UploadError):
    """MAX отверг саму запись (AUDIO_VALIDATION_FAILED и т.п.), ретраи бесполезны."""


def _upload_user_agent(config) -> str:
    ua = getattr(getattr(config, "device", None), "user_agent", None)
    header_ua = getattr(ua, "header_user_agent", None)
    if header_ua:  # WebClient: тот же браузерный UA, что ушёл в handshake
        return header_ua
    version = getattr(ua, "app_version", None) or getattr(config, "app_version", None) or ""
    return (
        f"OKMessages/{version} ({getattr(ua, 'os_version', '')};"
        f" {getattr(ua, 'device_name', '')}; {getattr(ua, 'screen', '')})"
    )


async def _upload_voice(self, voice):
    """Замена ``UploadService.upload_voice``."""
    from pymax.api.uploads.models import VideoUploadResponse
    from pymax.api.uploads.payloads import UploadPayload, VoiceAttachPayload
    from pymax.protocol import Opcode

    body = await voice.read()
    try:
        data = await self.app.invoke(
            Opcode.VIDEO_UPLOAD, payload=UploadPayload(type=2, uploader_type=1).to_payload()
        )
        slot = VideoUploadResponse.model_validate(data.payload).info[0]
    except Exception as e:
        raise pymax.UploadError("Не удалось получить upload URL для голосового") from e

    form = aiohttp.FormData()
    form.add_field("file", body, filename=voice.name or "voice.ogg", content_type="audio/ogg")
    timeout = aiohttp.ClientTimeout(total=self.app.config.upload_timeout or None, sock_read=60)
    headers = {"User-Agent": _upload_user_agent(self.app.config)}

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, proxy=self.app.config.proxy) as session,
            session.post(slot.url, data=form, headers=headers) as resp,
        ):
            # Ошибки MAX отдаёт с HTTP 200 в теле ответа
            text = (await resp.text(errors="replace"))[:300]
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise pymax.UploadError(f"Голосовое не загрузилось: {e!r}") from e

    if resp.status != HTTPStatus.OK:
        raise pymax.UploadError(f"Голосовое не загрузилось: HTTP {resp.status} {text}")
    if "error_code" in text:
        raise VoiceRejected(f"MAX отверг голосовое: {text}")

    # token="" — тогда PyMax сериализует аттач как {_type: AUDIO, audioId: ...}
    return VoiceAttachPayload(
        video_id=slot.video_id,
        token="",
        duration=await voice.get_duration(),
        wave=b"\x00" * 80,
    )


async def _upload_photo(self, photo, profile: bool = False):
    """Замена ``UploadService.upload_photo``: photoIds в URL больше не обязателен."""
    from pymax.api.response import payload_item
    from pymax.api.uploads.models import PhotoUploadResponse
    from pymax.api.uploads.payloads import AttachPhotoPayload, UploadPayload
    from pymax.protocol import Opcode

    try:
        data = await self.app.invoke(
            Opcode.PHOTO_UPLOAD, payload=UploadPayload(profile=profile).to_payload()
        )
        url = payload_item(data, "url", str)
    except Exception as e:
        raise pymax.UploadError("Не удалось получить upload URL для фото") from e
    if not url:
        raise pymax.UploadError("MAX не выдал upload URL для фото")

    photo_id = (parse_qs(urlparse(url).query).get("photoIds") or [None])[0]
    ext, mime = photo.validate_photo()

    form = aiohttp.FormData()
    form.add_field("file", await photo.read(), filename=f"image.{ext}", content_type=mime)
    try:
        async with (
            aiohttp.ClientSession(proxy=self.app.config.proxy) as session,
            session.post(url, data=form) as resp,
        ):
            if resp.status != HTTPStatus.OK:
                raise pymax.UploadError(f"Фото не загрузилось: HTTP {resp.status}")
            result = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise pymax.UploadError(f"Фото не загрузилось: {e!r}") from e

    photos = PhotoUploadResponse.model_validate(result).photos
    if photo_id is not None and str(photo_id) in photos:
        token = photos[str(photo_id)].token
    elif len(photos) == 1:
        token = next(iter(photos.values())).token
    else:
        raise pymax.UploadError(f"Не понял ответ на загрузку фото: {sorted(photos)}")
    return AttachPhotoPayload(photo_token=token)


def _resolve_attach(frame):
    """Замена ``resolve_attach``: аудио проверяем раньше видео.

    Уведомление о голосовом содержит и videoId, и audioId, поэтому оригинал
    классифицирует его как VIDEO_READY.
    """
    from pydantic import ValidationError
    from pymax.dispatch.enums import EventType
    from pymax.types import AudioUploadSignal
    from pymax.types.events import FileUploadSignal, VideoUploadSignal

    for model, event in (
        (FileUploadSignal, EventType.FILE_READY),
        (AudioUploadSignal, EventType.VOICE_READY),
        (VideoUploadSignal, EventType.VIDEO_READY),
    ):
        try:
            model.model_validate(frame.payload)
            return event
        except ValidationError:
            pass
    return None


def apply() -> None:
    global _applied
    if _applied:
        return
    if not pymax.__version__.startswith("2.4."):
        log.warning(
            "Патчи писались под maxapi-python 2.4.x, установлена %s — пропускаю",
            pymax.__version__,
        )
        return

    from pymax.api.uploads.service import UploadService
    from pymax.dispatch import mapping
    from pymax.protocol import Opcode

    UploadService.upload_voice = _upload_voice
    UploadService.upload_photo = _upload_photo
    mapping.EVENT_MAP[Opcode.NOTIF_ATTACH] = _resolve_attach
    _applied = True
    log.debug("Патчи PyMax %s применены", pymax.__version__)
