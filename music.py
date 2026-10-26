"""Validate MP3 uploads and publish complete files into a shared music directory."""

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from mutagen.mp3 import MP3
from mutagen import MutagenError
from mutagen.id3 import APIC, TIT2, TPE1

from integrations import IntegrationError


# The hosted Telegram Bot API can only download files up to 20 MB.
MAX_MP3_BYTES = 20 * 1024 * 1024
MAX_COVER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TrackMetadata:
    title: str | None = None
    performer: str | None = None
    cover: bytes | None = None


@dataclass(frozen=True)
class ImportedTrack:
    filename: str
    duplicate: bool
    metadata_updated: bool = False
    missing_metadata: tuple[str, ...] = ()


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


def has_cover(path: Path) -> bool:
    try:
        tags = MP3(path).tags
        return bool(tags and any(frame.data for frame in tags.getall("APIC")))
    except (MutagenError, ValueError, EOFError):
        raise IntegrationError("Файл не удалось прочитать как MP3. Проверьте формат и целостность.") from None


def missing_metadata(path: Path) -> tuple[str, ...]:
    tags = MP3(path).tags
    missing = []
    if not tags or not any(str(value).strip() for frame in tags.getall("TPE1") for value in frame.text):
        missing.append("исполнитель")
    if not tags or not any(frame.data for frame in tags.getall("APIC")):
        missing.append("обложка")
    return tuple(missing)


def complete_metadata(path: Path, metadata: TrackMetadata) -> bool:
    """Only fill empty ID3 fields. Existing artwork and other frames take precedence."""
    audio = MP3(path, translate=False)
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    version = tags.version[1] if tags.version[1] in (3, 4) else 4
    changed = False
    for key, cls, value in (("TIT2", TIT2, metadata.title), ("TPE1", TPE1, metadata.performer)):
        if value and value.strip() and not any(str(t).strip() for frame in tags.getall(key) for t in frame.text):
            tags.add(cls(encoding=3, text=[value.strip()]))
            changed = True
    if metadata.cover and not any(frame.data for frame in tags.getall("APIC")):
        data = metadata.cover
        if len(data) > MAX_COVER_BYTES:
            raise IntegrationError("Обложка из Telegram слишком большая (больше 1 МиБ).")
        if data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        else:
            raise IntegrationError("Telegram передал обложку в неподдерживаемом формате. Нужен JPEG или PNG.")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Telegram cover", data=data))
        changed = True
    if changed:
        tags.save(path, v2_version=version)
    return changed


def preserve_original(source: Path, directory: Path, digest: str) -> None:
    """Keep exact incoming bytes outside the set of playable MP3 filenames."""
    originals = directory / ".originals"
    originals.mkdir(mode=0o750, exist_ok=True)
    destination = originals / f"{digest}.original"
    if destination.exists():
        return
    with tempfile.NamedTemporaryFile(dir=originals, suffix=".part", delete=False) as stream:
        staging = Path(stream.name)
    try:
        shutil.copyfile(source, staging)
        staging.chmod(0o640)
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(staging, destination)
        except FileExistsError:
            pass
    finally:
        staging.unlink(missing_ok=True)


def publish_mp3(temporary: Path, directory: Path, filename: str,
                metadata: TrackMetadata | None = None) -> ImportedTrack:
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
        destination = existing or directory / f"{upload_name(filename, None)[:-4]}-{digest}.mp3"
        if metadata:
            # Work on a separate inode: never edit a published track in place. Re-sending
            # an older upload fills missing fields while retaining existing richer tags.
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".shrimp-tags-", suffix=".part", delete=False) as stream:
                staging = Path(stream.name)
            try:
                shutil.copyfile(existing or temporary, staging)
                changed = complete_metadata(staging, metadata)
                missing = missing_metadata(staging)
                if changed:
                    preserve_original(temporary, directory, digest)
                    staging.chmod(0o644)
                    with staging.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(staging, destination)
                    return ImportedTrack(destination.name, bool(existing), True, missing)
            finally:
                staging.unlink(missing_ok=True)
        else:
            missing = ()
        if existing:
            return ImportedTrack(existing.name, True, False, missing)
        temporary.chmod(0o644)
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return ImportedTrack(destination.name, True, False, missing)
        return ImportedTrack(destination.name, False, False, missing)
    except (MutagenError, ValueError, EOFError):
        raise IntegrationError("Не удалось сохранить метаданные MP3. Исходный файл не заменён.") from None
    except OSError:
        raise IntegrationError("Не удалось сохранить MP3. Проверьте папку NAVIDROME_IMPORT_DIR, права и свободное место.") from None
