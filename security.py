"""Access checks, best-effort redaction and single-use callback tokens."""

import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from settings import Settings


def authorized(settings: Settings, user_id: int, chat_id: int) -> bool:
    return user_id in settings.allowed_users and (
        not settings.allowed_chats or chat_id in settings.allowed_chats
    )


def redact(text: str, settings: Settings) -> str:
    for secret in (settings.token, settings.dokploy_key, settings.ha_token):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[\w.\-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(
        r'''(?i)(["']?[\w.-]*(?:token|password|secret|api[_-]?key|authorization)[\w.-]*["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''',
        r"\1[REDACTED]", text,
    )
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


@dataclass
class Ticket:
    user_id: int
    chat_id: int
    message_id: int
    action: str
    payload: Any
    expires: float


class Tickets:
    def __init__(self) -> None:
        self.items: dict[str, Ticket] = {}

    def issue(self, user_id: int, chat_id: int, message_id: int, action: str,
              payload: Any, ttl: float = 600) -> str:
        now = time.monotonic()
        self.items = {key: item for key, item in self.items.items() if item.expires > now}
        while len(self.items) >= 2048:
            self.items.pop(next(iter(self.items)))
        key = secrets.token_urlsafe(12)
        self.items[key] = Ticket(user_id, chat_id, message_id, action, payload, now + ttl)
        return "ui:" + key

    def take(self, data: str, user_id: int, chat_id: int, message_id: int) -> Ticket | None:
        key = data.removeprefix("ui:")
        item = self.items.get(key)
        if not item or item.expires <= time.monotonic():
            self.items.pop(key, None)
            return None
        if (item.user_id, item.chat_id, item.message_id) != (user_id, chat_id, message_id):
            return None
        return self.items.pop(key)

    def revoke_message(self, chat_id: int, message_id: int) -> None:
        self.items = {key: item for key, item in self.items.items()
                      if (item.chat_id, item.message_id) != (chat_id, message_id)}
