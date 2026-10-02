"""MAX ↔ Telegram форвардер.

    python3 main.py                # всё в одном процессе (ROLE из config.py)
    python3 main.py --role tg      # только Telegram-половина (сервер за рубежом)
    python3 main.py --role max     # только MAX-половина (сервер в РФ)
    python3 main.py --mute         # только MAX → TG
"""

import argparse
import asyncio
import logging
import signal
import stat
import sys
from pathlib import Path

from forwarder import settings as settings_mod
from forwarder.link import LocalLink, WsLink

log = logging.getLogger("main")


def build(s: settings_mod.Settings):
    """Возвращает (половинки, корутины для запуска)."""
    if s.ROLE == "both":
        from forwarder.max_side import MaxSide
        from forwarder.tg_side import TgSide

        tg_link, max_link = LocalLink.pair()
        sides = [TgSide(s, tg_link), MaxSide(s, max_link)]
        return sides, [side.run() for side in sides]

    link = WsLink(
        role=s.ROLE,
        secret=s.LINK_SECRET,
        listen=s.LINK_LISTEN,
        url=s.LINK_URL,
        tls_cert=str(s.path(s.LINK_TLS_CERT)) if s.LINK_TLS_CERT else "",
        tls_key=str(s.path(s.LINK_TLS_KEY)) if s.LINK_TLS_KEY else "",
        tls_ca=str(s.path(s.LINK_TLS_CA)) if s.LINK_TLS_CA else "",
        proxy=s.LINK_PROXY,
        allow_ips=s.LINK_ALLOW_IPS,
    )
    if s.ROLE == "tg":
        from forwarder.tg_side import TgSide as Side
    else:
        from forwarder.max_side import MaxSide as Side
    side = Side(s, link)
    return [side], [link.run(), side.run()]


def warn_open_secrets(s: settings_mod.Settings, config: Path) -> None:
    files = [config] + [s.path(f) for f in (s.LINK_TLS_KEY,) if f]
    for f in files:
        try:
            mode = f.stat().st_mode
        except OSError:
            continue
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("%s читают другие пользователи — сделай: chmod 600 %s", f.name, f)


async def run(s: settings_mod.Settings) -> None:
    sides, coros = build(s)
    tasks = [asyncio.create_task(c) for c in coros]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t is not stopper and not t.cancelled() and t.exception():
            log.error("Упало: %r", t.exception(), exc_info=t.exception())

    print("\nОстанавливаемся...")
    for side in sides:
        try:
            await asyncio.wait_for(side.close(), 10)
        except Exception:
            pass
    for t in [*tasks, stopper]:
        t.cancel()
    await asyncio.gather(*tasks, stopper, return_exceptions=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Пересылка сообщений MAX ↔ Telegram")
    ap.add_argument("--role", choices=settings_mod.ROLES, help="переопределить ROLE из конфига")
    ap.add_argument("--mute", action="store_true", help="только MAX → Telegram")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.py")))
    args = ap.parse_args()

    s = settings_mod.load(Path(args.config), args.role, args.mute)
    logging.basicConfig(
        level=s.LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    errors = s.validate()
    if errors:
        for e in errors:
            print(f"config: {e}", file=sys.stderr)
        sys.exit(1)

    warn_open_secrets(s, Path(args.config))
    print(f"Роль: {s.ROLE}" + (" | 🔇 --mute: TG → MAX отключён" if s.MUTE else ""))
    asyncio.run(run(s))


if __name__ == "__main__":
    main()
