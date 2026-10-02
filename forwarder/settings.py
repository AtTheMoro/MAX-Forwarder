"""Загрузка config.py с дефолтами и проверкой под роль."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, fields
from pathlib import Path

ROLES = ("both", "tg", "max")


@dataclass
class Settings:
    ROLE: str = "both"
    MUTE: bool = False
    LOG_LEVEL: str = "INFO"
    MAX_FILE_MB: int = 50

    MAX_AUTH: str = ""
    MAX_TOKEN: str = ""
    MAX_PHONE: str = ""
    MAX_CHAT_ID: int = 0
    MAX_SELF_ID: int | None = None
    MAX_SKIP_OWN: bool = False
    MAX_WORK_DIR: str = "max_session"
    MAX_PROXY: str | None = None

    TG_TOKEN: str = ""
    TG_CHAT_ID: int | str = ""
    TG_PROXY: str | None = None

    LINK_LISTEN: str = ""
    LINK_URL: str = ""
    LINK_SECRET: str = ""
    LINK_TLS_CERT: str = ""
    LINK_TLS_KEY: str = ""
    LINK_TLS_CA: str = ""
    LINK_PROXY: str | None = None

    base_dir: Path = Path(".")

    @property
    def runs_tg(self) -> bool:
        return self.ROLE in ("both", "tg")

    @property
    def runs_max(self) -> bool:
        return self.ROLE in ("both", "max")

    @property
    def max_file_bytes(self) -> int:
        return int(self.MAX_FILE_MB) * 1024 * 1024

    def path(self, name: str) -> Path:
        p = Path(name)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def max_auth(self) -> str:
        if self.MAX_AUTH:
            return self.MAX_AUTH.lower()
        return "token" if self.MAX_TOKEN else "qr"

    def validate(self) -> list[str]:
        errors = []
        if self.ROLE not in ROLES:
            errors.append(f"ROLE должен быть одним из {ROLES}, а не {self.ROLE!r}")
            return errors
        if self.runs_max:
            if not self.MAX_CHAT_ID:
                errors.append("MAX_CHAT_ID не задан")
            if self.max_auth not in ("token", "qr", "sms"):
                errors.append("MAX_AUTH: token | qr | sms")
            if self.max_auth == "token" and not self.MAX_TOKEN:
                errors.append("MAX_AUTH=token, но MAX_TOKEN пустой")
            if self.max_auth == "sms" and not self.MAX_PHONE:
                errors.append("MAX_AUTH=sms, но MAX_PHONE пустой")
        if self.runs_tg:
            if not self.TG_TOKEN:
                errors.append("TG_TOKEN не задан")
            if not str(self.TG_CHAT_ID):
                errors.append("TG_CHAT_ID не задан")
        if self.ROLE != "both":
            if bool(self.LINK_LISTEN) == bool(self.LINK_URL):
                errors.append("Для раздельного запуска задай ровно одно: LINK_LISTEN или LINK_URL")
            if len(self.LINK_SECRET) < 16:
                errors.append("LINK_SECRET пустой или короче 16 символов")
            if bool(self.LINK_TLS_CERT) != bool(self.LINK_TLS_KEY):
                errors.append("LINK_TLS_CERT и LINK_TLS_KEY задаются вместе")
        return errors


def load(config_path: Path, role_override: str | None = None, mute: bool = False) -> Settings:
    spec = importlib.util.spec_from_file_location("forwarder_user_config", config_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Не могу прочитать {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    s = Settings(base_dir=config_path.resolve().parent)
    for f in fields(Settings):
        if f.name.isupper() and hasattr(module, f.name):
            setattr(s, f.name, getattr(module, f.name))
    if role_override:
        s.ROLE = role_override
    s.ROLE = str(s.ROLE).lower()
    s.MUTE = bool(s.MUTE or mute)
    if s.MAX_CHAT_ID:
        s.MAX_CHAT_ID = int(s.MAX_CHAT_ID)
    return s
