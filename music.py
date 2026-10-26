"""Validate MP3 uploads and publish complete files into a shared music directory."""

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from mutagen.mp3 import MP3
from mutagen import MutagenError
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TPE2

from integrations import IntegrationError


# The hosted Telegram Bot API can only download files up to 20 MB.
MAX_MP3_BYTES = 20 * 1024 * 1024
MAX_COVER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TrackMetadata:
    title: str | None = None
    performer: str | None = None
    cover: bytes | None = None
    album: str | None = None
    album_artist: str | None = None


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


def complete_metadata(path: Path, metadata: TrackMetadata, *, overwrite: bool = False) -> bool:
    """Fill empty fields on import; overwrite only explicit fields for manual edits."""
    audio = MP3(path, translate=False)
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    version = tags.version[1] if tags.version[1] in (3, 4) else 4
    changed = False
    for key, cls, value in (("TIT2", TIT2, metadata.title), ("TPE1", TPE1, metadata.performer),
                            ("TALB", TALB, metadata.album), ("TPE2", TPE2, metadata.album_artist)):
        existing = [str(t).strip() for frame in tags.getall(key) for t in frame.text if str(t).strip()]
        if value and value.strip() and (not existing or (overwrite and existing != [value.strip()])):
            tags.add(cls(encoding=3, text=[value.strip()]))
            changed = True
    pictures = tags.getall("APIC")
    front = [p for p in pictures if p.type == 3]
    if metadata.cover and (not any(frame.data for frame in pictures) or
                           (overwrite and not any(p.data == metadata.cover for p in front))):
        data = metadata.cover
        if len(data) > MAX_COVER_BYTES:
            raise IntegrationError("Обложка из Telegram слишком большая (больше 1 МиБ).")
        if data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        else:
            raise IntegrationError("Telegram передал обложку в неподдерживаемом формате. Нужен JPEG или PNG.")
        if overwrite:
            # Replace the front cover only; retain booklets and other embedded images.
            tags.setall("APIC", [p for p in pictures if p.type != 3])
        description = "Telegram cover"
        used = {p.desc for p in tags.getall("APIC")}
        while description in used:
            description += " (front)"
        tags.add(APIC(encoding=3, mime=mime, type=3, desc=description, data=data))
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


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def track_id(filename: str) -> str:
    match = re.fullmatch(r".+-([0-9a-f]{64})\.mp3", filename)
    if not match:
        raise IntegrationError("Не удалось определить загруженный трек.")
    return match[1]


def find_track(directory: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise IntegrationError("Некорректная ссылка на трек.")
    paths = list(directory.glob(f"*-{digest}.mp3"))
    if len(paths) != 1 or paths[0].is_symlink() or not paths[0].is_file():
        raise IntegrationError("Трек не найден в загрузках Navidrome. Сначала отправьте MP3 боту.")
    return paths[0]


def message_track(directory: Path, chat_id: int, message_id: int, digest: str | None = None) -> str | None:
    """Persist reply associations across restarts without putting file paths in Telegram."""
    db = directory / ".shrimp-messages.sqlite3"
    if digest is None and not db.exists():
        return None
    if digest is not None:
        find_track(directory, digest)
    try:
        with closing(sqlite3.connect(db, timeout=5)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS messages (chat INTEGER, message INTEGER, digest TEXT NOT NULL, PRIMARY KEY(chat, message))")
            if digest is not None:
                connection.execute("INSERT INTO messages VALUES (?, ?, ?) ON CONFLICT(chat, message) DO UPDATE SET digest=excluded.digest",
                                   (chat_id, message_id, digest))
                db.chmod(0o600)
                return digest
            row = connection.execute("SELECT digest FROM messages WHERE chat=? AND message=?", (chat_id, message_id)).fetchone()
            return row[0] if row else None
    except (OSError, sqlite3.Error):
        raise IntegrationError("Не удалось прочитать привязку сообщения к треку. Проверьте папку загрузок.") from None


def parse_metadata_edit(text: str) -> TrackMetadata:
    body = re.sub(r"^/tags(?:@[A-Za-z0-9_]+)?(?:\s|$)", "", text.strip(), count=1)
    aliases = {"название": "title", "title": "title", "исполнитель": "performer", "artist": "performer",
               "альбом": "album", "album": "album", "исполнитель альбома": "album_artist", "album artist": "album_artist"}
    values = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        field = aliases.get(key.strip().casefold())
        if not separator or not field:
            raise IntegrationError("Каждое поле пишите с новой строки: Название: …, Исполнитель: …, Альбом: …, Исполнитель альбома: …")
        if field in values or not value.strip() or len(value.strip()) > 300:
            raise IntegrationError("Укажите каждое поле один раз и непустым значением до 300 символов. Ненужные поля пропустите.")
        values[field] = value.strip()
    return TrackMetadata(**values)


def read_track_metadata(directory: Path, digest: str) -> dict:
    try:
        tags = MP3(find_track(directory, digest)).tags
        result = {field: str(tags.get(key, "")) if tags else "" for field, key in
                  (("title", "TIT2"), ("performer", "TPE1"), ("album", "TALB"), ("album_artist", "TPE2"))}
        result["cover"] = bool(tags and any(p.data for p in tags.getall("APIC")))
        return result
    except (OSError, MutagenError, ValueError, EOFError):
        raise IntegrationError("Не удалось прочитать метаданные трека.") from None


def edit_track(directory: Path, digest: str, metadata: TrackMetadata) -> bool:
    path = find_track(directory, digest)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".shrimp-edit-", suffix=".part", delete=False) as stream:
            staging = Path(stream.name)
        shutil.copyfile(path, staging)
        if not complete_metadata(staging, metadata, overwrite=True):
            return False
        preserve_original(path, directory, digest)
        staging.chmod(0o644)
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(staging, path)
        return True
    except (OSError, MutagenError, ValueError, EOFError):
        raise IntegrationError("Не удалось обновить метаданные. Существующий трек не заменён.") from None
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
