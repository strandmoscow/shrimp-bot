"""Validate MP3 uploads and publish complete files into a shared music directory."""

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from mutagen.mp3 import MP3
from mutagen import MutagenError

from integrations import IntegrationError


# The hosted Telegram Bot API can only download files up to 20 MB.
MAX_MP3_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ImportedTrack:
    filename: str
    duplicate: bool


def upload_name(filename: str | None, mime_type: str | None, title: str | None = None) -> str:
    if filename:
        # Treat both Windows and Unix separators as untrusted path components.
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not name.lower().endswith(".mp3"):
            raise IntegrationError("Пришлите MP3 как аудио или как файл с расширением .mp3.")
    elif mime_type in {"audio/mpeg", "audio/mp3"}:
        name = (title or "Telegram audio") + ".mp3"
    else:
        raise IntegrationError("Пришлите MP3 как аудио или как файл с расширением .mp3.")
    stem = unicodedata.normalize("NFC", name[:-4])
    stem = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", stem).strip(" .")
    # Leave room for the full content hash and the extension on common filesystems.
    stem = stem.encode("utf-8")[:120].decode("utf-8", errors="ignore").strip(" .")
    return (stem or "Telegram audio") + ".mp3"


def publish_mp3(temporary: Path, directory: Path, filename: str) -> ImportedTrack:
    """Publish atomically; the scanner must never see a partially downloaded MP3."""
    try:
        size = temporary.stat().st_size
        if not 0 < size <= MAX_MP3_BYTES:
            raise IntegrationError("MP3 должен быть непустым и размером не больше 20 МБ.")
        try:
            audio = MP3(temporary)
            if audio.info.layer != 3 or audio.info.length <= 0:
                raise ValueError("Not MP3 audio")
        except (MutagenError, ValueError, EOFError):
            raise IntegrationError("Файл не удалось прочитать как MP3. Проверьте формат и целостность.") from None
        with temporary.open("rb") as source:
            checksum = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(chunk)
            digest = checksum.hexdigest()
        # Dedupe exact bytes even when Telegram supplies a different filename.
        existing = next(directory.glob(f"*-{digest}.mp3"), None)
        if existing:
            return ImportedTrack(existing.name, True)
        destination = directory / f"{upload_name(filename, None)[:-4]}-{digest}.mp3"
        temporary.chmod(0o644)
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return ImportedTrack(destination.name, True)
        return ImportedTrack(destination.name, False)
    except OSError:
        raise IntegrationError("Не удалось сохранить MP3. Проверьте папку NAVIDROME_IMPORT_DIR, права и свободное место.") from None
