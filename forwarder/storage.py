"""JSON-файлы состояния: имена, маппинг сообщений."""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger("storage")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("Не прочитать %s (%s), начинаю с пустого", path.name, e)
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_names(path: Path) -> dict[str, str]:
    """custom_names.json / tg_names.json: ключи с "_" — комментарии."""
    data = read_json(path, {})
    return {str(k): str(v) for k, v in data.items() if not str(k).startswith("_")}


class MsgMap:
    """Связь id сообщений MAX ↔ Telegram (для ответов). Живёт на TG-ноде."""

    def __init__(self, path: Path, limit: int = 20000) -> None:
        self.path = path
        self.limit = limit
        data = read_json(path, {})
        self.m2t: OrderedDict[str, int] = OrderedDict(
            (str(k), int(v)) for k, v in data.get("m2t", {}).items()
        )
        self.t2m: OrderedDict[str, str] = OrderedDict(
            (str(k), str(v)) for k, v in data.get("t2m", {}).items()
        )

    def add(self, max_id, tg_ids: list[int]) -> None:
        if not max_id or not tg_ids:
            return
        self.m2t[str(max_id)] = int(tg_ids[0])
        for tg_id in tg_ids:
            self.t2m[str(tg_id)] = str(max_id)
        for d in (self.m2t, self.t2m):
            while len(d) > self.limit:
                d.popitem(last=False)
        try:
            write_json(self.path, {"m2t": self.m2t, "t2m": self.t2m})
        except OSError as e:
            log.warning("Не сохранить %s: %s", self.path.name, e)

    def tg(self, max_id) -> int | None:
        return self.m2t.get(str(max_id)) if max_id else None

    def max(self, tg_id) -> str | None:
        return self.t2m.get(str(tg_id)) if tg_id else None


class BoundedSet:
    def __init__(self, limit: int = 5000) -> None:
        self._d: OrderedDict = OrderedDict()
        self.limit = limit

    def add(self, item) -> None:
        self._d[item] = None
        self._d.move_to_end(item)
        while len(self._d) > self.limit:
            self._d.popitem(last=False)

    def __contains__(self, item) -> bool:
        return item in self._d
