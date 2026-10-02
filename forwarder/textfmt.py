"""Форматирование текста: entities Telegram ↔ elements MAX.

Между нодами форматирование ездит в виде TG-подобных dict
``{"type", "offset", "length", "url"?}`` — смещения в UTF-16, как у обоих.
"""

from __future__ import annotations

TG_TO_MAX = {
    "bold": "STRONG",
    "italic": "EMPHASIZED",
    "underline": "UNDERLINE",
    "strikethrough": "STRIKETHROUGH",
    "code": "MONOSPACED",
    "pre": "CODE",
    "text_link": "LINK",
    "blockquote": "QUOTE",
    "expandable_blockquote": "QUOTE",
}
MAX_TO_TG = {
    "STRONG": "bold",
    "EMPHASIZED": "italic",
    "UNDERLINE": "underline",
    "STRIKETHROUGH": "strikethrough",
    "MONOSPACED": "code",
    "CODE": "pre",
    "LINK": "text_link",
    "QUOTE": "blockquote",
    "HEADING": "bold",
}


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def shift(entities: list[dict], by: int) -> list[dict]:
    return [{**e, "offset": e["offset"] + by} for e in entities]


def join(parts: list[tuple[str, list[dict]]], sep: str = "\n") -> tuple[str, list[dict]]:
    """Склеивает куски текста с их entities."""
    text, ents = "", []
    for chunk, chunk_ents in parts:
        if not chunk:
            continue
        if text:
            text += sep
        ents += shift(chunk_ents, u16(text))
        text += chunk
    return text, ents


def from_tg(entities) -> list[dict]:
    """aiogram MessageEntity → нейтральный формат."""
    out = []
    for e in entities or []:
        if e.type not in TG_TO_MAX or (e.type == "text_link" and not e.url):
            continue
        item = {"type": e.type, "offset": e.offset, "length": e.length}
        if e.url:
            item["url"] = e.url
        out.append(item)
    return out


def to_max(entities: list[dict]) -> list[dict]:
    out = []
    for e in entities:
        kind = TG_TO_MAX.get(e["type"])
        if not kind:
            continue
        el = {"type": kind, "from": e["offset"], "length": e["length"]}
        if kind == "LINK":
            el["attributes"] = {"url": e["url"]}
        out.append(el)
    return out


def from_max(elements) -> list[dict]:
    out = []
    for el in elements or []:
        kind = MAX_TO_TG.get(str(el.get("type", "")).upper())
        length = el.get("length") or 0
        if not kind or length <= 0:
            continue
        item = {"type": kind, "offset": el.get("from") or 0, "length": length}
        if kind == "text_link":
            url = (el.get("attributes") or {}).get("url")
            if not url:
                continue
            item["url"] = url
        out.append(item)
    return out
