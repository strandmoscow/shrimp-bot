"""Configuration only; importing this module never reads tokens or contacts services."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class BackupTarget:
    name: str
    id: str
    kind: str = "backup"
    max_age_hours: float = 26


@dataclass(frozen=True)
class Settings:
    token: str = field(default="", repr=False)
    allowed_users: frozenset[int] = frozenset()
    allowed_chats: frozenset[int] = frozenset()
    dokploy_url: str = "https://dokploy.strand.moscow"
    dokploy_key: str = field(default="", repr=False)
    health_urls: dict[str, str] = field(default_factory=dict)
    ssh_host: str = ""
    ssh_user: str = "alex"
    ssh_port: int = 22
    ssh_key_file: str = ""
    ssh_known_hosts: str = ""
    disk_path: str = "/"
    vpn_interface: str = "tg-vpn"
    minecraft_host: str = ""
    minecraft_port: int = 25565
    ha_url: str = ""
    ha_token: str = field(default="", repr=False)
    ha_entities: tuple[str, ...] = ()
    ha_scenes: tuple[str, ...] = ()
    backups: tuple[BackupTarget, ...] = ()
    alert_chat: int | None = None
    alert_thread: int | None = None
    backup_check_seconds: int = 3600
    navidrome_import_dir: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        raw = json.loads(e.get("BACKUP_TARGETS", "[]"))
        if not isinstance(raw, list):
            raise ValueError("BACKUP_TARGETS должен быть JSON-массивом.")
        targets = tuple(BackupTarget(**item) for item in raw)
        if len({t.name.casefold() for t in targets}) != len(targets):
            raise ValueError("Имена BACKUP_TARGETS должны быть уникальны.")
        for target in targets:
            if not target.name.strip() or not target.id.strip():
                raise ValueError("У каждого бэкапа нужны name и id.")
            if target.kind not in {"backup", "volumeBackup"} or target.max_age_hours <= 0:
                raise ValueError("Проверьте kind и max_age_hours в BACKUP_TARGETS.")
        s = cls(
            token=e.get("BOT_TOKEN", "").strip(),
            allowed_users=frozenset(int(x) for x in csv(e.get("ALLOWED_USER_IDS", ""))),
            allowed_chats=frozenset(int(x) for x in csv(e.get("ALLOWED_CHAT_IDS", ""))),
            dokploy_url=e.get("DOKPLOY_URL", cls.dokploy_url).rstrip("/"),
            dokploy_key=e.get("DOKPLOY_API_KEY", "").strip(),
            health_urls=json.loads(e.get("HEALTH_URLS", "{}")),
            ssh_host=e.get("SERVER_SSH_HOST", "").strip(),
            ssh_user=e.get("SERVER_SSH_USER", "alex").strip(),
            ssh_port=int(e.get("SERVER_SSH_PORT", "22")),
            ssh_key_file=e.get("SERVER_SSH_KEY_FILE", "").strip(),
            ssh_known_hosts=e.get("SERVER_SSH_KNOWN_HOSTS_FILE", "").strip(),
            disk_path=e.get("SERVER_DISK_PATH", "/"),
            vpn_interface=e.get("VPN_INTERFACE", "tg-vpn"),
            minecraft_host=e.get("MINECRAFT_HOST", "").strip(),
            minecraft_port=int(e.get("MINECRAFT_PORT", "25565")),
            ha_url=e.get("HA_URL", "").rstrip("/"),
            ha_token=e.get("HA_TOKEN", "").strip(),
            ha_entities=csv(e.get("HA_ENTITIES", "")),
            ha_scenes=csv(e.get("HA_SCENES", "")),
            backups=targets,
            alert_chat=int(e["BACKUP_ALERT_CHAT_ID"]) if e.get("BACKUP_ALERT_CHAT_ID") else None,
            alert_thread=int(e["BACKUP_ALERT_THREAD_ID"]) if e.get("BACKUP_ALERT_THREAD_ID") else None,
            backup_check_seconds=int(e.get("BACKUP_CHECK_SECONDS", "3600")),
            navidrome_import_dir=e.get("NAVIDROME_IMPORT_DIR", "").strip(),
        )
        if any(uid <= 0 for uid in s.allowed_users):
            raise ValueError("ALLOWED_USER_IDS содержит ID пользователей, а не групп.")
        if not isinstance(s.health_urls, dict) or any(
            not isinstance(name, str) or not isinstance(url, str)
            or not url.startswith(("https://", "http://"))
            for name, url in s.health_urls.items()
        ):
            raise ValueError("HEALTH_URLS должен быть JSON-объектом с HTTP(S)-адресами.")
        if s.ssh_host and not re.fullmatch(r"[A-Za-z0-9_.:-]+", s.ssh_host):
            raise ValueError("SERVER_SSH_HOST должен содержать только имя хоста или IP.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", s.ssh_user):
            raise ValueError("Некорректный SERVER_SSH_USER.")
        if not 0 < s.ssh_port < 65536 or not 0 < s.minecraft_port < 65536:
            raise ValueError("Порт должен быть от 1 до 65535.")
        if s.backup_check_seconds < 60:
            raise ValueError("BACKUP_CHECK_SECONDS должен быть не меньше 60.")
        if s.navidrome_import_dir and not Path(s.navidrome_import_dir).is_absolute():
            raise ValueError("NAVIDROME_IMPORT_DIR должен быть абсолютным путём внутри контейнера.")
        if s.alert_chat and s.allowed_chats and s.alert_chat not in s.allowed_chats:
            raise ValueError("Добавьте BACKUP_ALERT_CHAT_ID в ALLOWED_CHAT_IDS.")
        if s.alert_chat and not s.allowed_users:
            raise ValueError("Для оповещений сначала заполните ALLOWED_USER_IDS.")
        for entity in (*s.ha_entities, *s.ha_scenes):
            if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity):
                raise ValueError("HA_ENTITIES и HA_SCENES должны содержать entity_id.")
        if any(x.split(".")[0] not in {"scene", "script"} for x in s.ha_scenes):
            raise ValueError("В HA_SCENES разрешены только scene.* и script.*.")
        return s


ROOT = Path(__file__).resolve().parent
