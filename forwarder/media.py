"""Перекодирование медиа через ffmpeg (если он есть) и скачивание файлов."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

import aiohttp
from yarl import URL

log = logging.getLogger("media")

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# MAX не принимает голосовые Telegram как есть (AUDIO_VALIDATION_FAILED),
# а тот же звук, пережатый в обычный Opus 48 кГц моно, — принимает.
VOICE_ARGS = ["-vn", "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1"]

# Рекомендованный PyMax формат кружков: квадрат 480, H.264 30 fps, AAC.
VIDEO_NOTE_ARGS = [
    "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480,fps=30",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "1024k", "-g", "30",
    "-c:a", "aac", "-b:a", "96k", "-t", "60", "-movflags", "+faststart",
]


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


async def ffmpeg(data: bytes, args: list[str], suffix: str, timeout: float = 120) -> bytes | None:
    """Прогоняет bytes через ffmpeg, None — если ffmpeg нет или он упал.

    Через временные файлы, а не pipe: mp4 с moov в конце из pipe не читается,
    а в pipe нормальный mp4 не записать.
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="mf-") as tmp:
        src, dst = Path(tmp, "in"), Path(tmp, "out" + suffix)
        src.write_bytes(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *args, str(dst),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            log.warning("ffmpeg не запустился: %s", e)
            return None
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("ffmpeg не уложился в %s с", timeout)
            return None
        if proc.returncode != 0 or not dst.exists():
            log.warning("ffmpeg rc=%s: %s", proc.returncode, err.decode(errors="replace")[:300])
            return None
        return dst.read_bytes() or None


async def to_max_voice(data: bytes) -> bytes:
    return await ffmpeg(data, VOICE_ARGS, ".ogg") or data


async def to_max_video_note(data: bytes) -> bytes:
    return await ffmpeg(data, VIDEO_NOTE_ARGS, ".mp4", timeout=300) or data


class TooBig(Exception):
    pass


class BadHost(Exception):
    pass


def check_host(url: str, allowed: list[str]) -> None:
    """Качаем только по https и только с CDN MAX — чтобы чужой URL не увёл запрос куда попало."""
    u = URL(url)
    host = (u.host or "").lower().rstrip(".")
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in allowed):
        raise BadHost(f"{u.scheme}://{host}")


async def download(url: str, limit: int, allowed_hosts: list[str], timeout: float = 300) -> bytes:
    """Скачивает файл с CDN MAX. TooBig — если больше limit, BadHost — если чужой хост."""
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://web.max.ru/"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        for _ in range(5):  # редиректы проверяем сами
            check_host(url, allowed_hosts)
            # okcdn подписывает URL как есть — yarl не должен его перекодировать
            target = URL(url, encoded=True) if ".okcdn.ru" in url else url
            async with session.get(target, headers=headers, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    url = str(URL(url).join(URL(resp.headers["Location"])))
                    continue
                resp.raise_for_status()
                if resp.content_length and resp.content_length > limit:
                    raise TooBig(resp.content_length)
                chunks, total = [], 0
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise TooBig(total)
                    chunks.append(chunk)
                return b"".join(chunks)
    raise aiohttp.ClientError("Слишком много редиректов")
