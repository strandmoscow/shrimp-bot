import os
import base64
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import BotUI, build_application
from integrations import IntegrationError
from music import MAX_MP3_BYTES, TrackMetadata, publish_mp3, upload_name
from mutagen.mp3 import MP3
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TXXX
from settings import Settings
from telegram.error import NetworkError
from telegram.ext import MessageHandler


# Generated 1-second 440 Hz sine wave, not a copyrighted recording.
FIXTURE = Path(__file__).parent / "fixtures/tone.mp3"
COVER = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7ioAAAAASUVORK5CYII=")


def audio_frames(path):
    data = path.read_bytes()
    if data.startswith(b"ID3"):
        size = 0
        for value in data[6:10]:
            size = (size << 7) | value
        data = data[10 + size:]
    if data[-128:-125] == b"TAG":
        data = data[:-128]
    return data


class MusicTests(unittest.TestCase):
    def test_untrusted_paths_and_unicode_names(self):
        for value in ("../../Трек.mp3", "C:\\music\\Трек.mp3"):
            self.assertEqual(upload_name(value, None), "Трек.mp3")
        self.assertEqual(upload_name(None, "audio/mpeg", "Мой трек"), "Мой трек.mp3")
        self.assertLess(len(upload_name("я" * 300 + ".mp3", None).encode()), 130)
        with self.assertRaises(IntegrationError):
            upload_name("song.exe", "audio/mpeg")

    def test_valid_file_is_published_without_overwriting_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            temp = root / ".incoming.part"
            shutil.copyfile(FIXTURE, temp)
            first = publish_mp3(temp, root, "Song.mp3")
            self.assertFalse(first.duplicate)
            self.assertEqual((root / first.filename).read_bytes(), FIXTURE.read_bytes())
            second = publish_mp3(temp, root, "A different filename.mp3")
            self.assertTrue(second.duplicate)
            self.assertEqual(first.filename, second.filename)
            self.assertEqual(len(list(root.glob("*.mp3"))), 1)
            # Same original name, different valid content must not overwrite the first track.
            temp.unlink()  # unlink before writing: publish uses a hard link
            temp.write_bytes(FIXTURE.read_bytes() + b"\0")
            third = publish_mp3(temp, root, "Song.mp3")
            self.assertFalse(third.duplicate)
            self.assertNotEqual(third.filename, first.filename)
            self.assertEqual((root / first.filename).read_bytes(), FIXTURE.read_bytes())

    def test_fake_and_oversized_mp3_never_enter_library(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            temp = root / ".incoming.part"
            for content in (b"", b"not music", b"ID3" + b"\0" * 200):
                temp.write_bytes(content)
                with self.assertRaises(IntegrationError):
                    publish_mp3(temp, root, "fake.mp3")
            with temp.open("wb") as stream:
                stream.truncate(MAX_MP3_BYTES + 1)
            with self.assertRaises(IntegrationError):
                publish_mp3(temp, root, "large.mp3")
            self.assertEqual(list(root.glob("*.mp3")), [])

    def test_relative_import_directory_is_rejected(self):
        with patch.dict(os.environ, {"NAVIDROME_IMPORT_DIR": "../music"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_embedded_metadata_artwork_and_exact_bytes_take_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            root, temp = Path(d), Path(d) / ".incoming.part"
            shutil.copyfile(FIXTURE, temp)
            tags = ID3(temp)
            tags.add(APIC(mime="image/png", type=3, desc="Original cover", data=COVER))
            tags.add(TALB(encoding=3, text=["Original album"]))
            tags.add(TXXX(encoding=3, desc="custom field", text=["Keep me"]))
            tags.save(temp, v2_version=3)
            original = temp.read_bytes()
            result = publish_mp3(temp, root, "Track.mp3", TrackMetadata("Wrong title", "Wrong artist", b"wrong cover"))
            self.assertEqual((root / result.filename).read_bytes(), original)
            self.assertFalse(result.metadata_updated)
            self.assertEqual(result.missing_metadata, ())
            self.assertFalse((root / ".originals").exists())

    def test_missing_fields_are_filled_and_original_bytes_and_audio_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root, temp = Path(d), Path(d) / ".incoming.part"
            shutil.copyfile(FIXTURE, temp)
            tags = ID3(temp)
            tags.delall("TIT2")
            tags.delall("TPE1")
            tags.add(TALB(encoding=3, text=["Existing album"]))
            tags.add(TXXX(encoding=3, desc="custom", text=["Keep me"]))
            tags.save(temp, v2_version=3)
            original, frames = temp.read_bytes(), audio_frames(temp)
            result = publish_mp3(temp, root, "Track.mp3", TrackMetadata("Название", "Исполнитель", COVER))
            published = root / result.filename
            tags = ID3(published)
            self.assertEqual(str(tags["TIT2"]), "Название")
            self.assertEqual(str(tags["TPE1"]), "Исполнитель")
            self.assertEqual(str(tags["TALB"]), "Existing album")
            self.assertEqual(str(tags["TXXX:custom"]), "Keep me")
            self.assertEqual(tags.version[1], 3)
            self.assertEqual(tags.getall("APIC")[0].data, COVER)
            self.assertEqual(audio_frames(published), frames)
            self.assertEqual(next((root / ".originals").iterdir()).read_bytes(), original)
            self.assertEqual(temp.read_bytes(), original)
            self.assertTrue(result.metadata_updated)
            self.assertEqual(result.missing_metadata, ())

    def test_resending_legacy_upload_repairs_it_without_duplicating_or_losing_tags(self):
        with tempfile.TemporaryDirectory() as d:
            root, temp = Path(d), Path(d) / ".incoming.part"
            shutil.copyfile(FIXTURE, temp)
            tags = ID3(temp)
            tags.delall("TPE1")
            tags.save(temp)
            first = publish_mp3(temp, root, "Track.mp3")
            repaired = publish_mp3(temp, root, "Renamed.mp3", TrackMetadata("Do not replace", "Artist", COVER))
            self.assertEqual(repaired.filename, first.filename)
            self.assertTrue(repaired.duplicate)
            self.assertTrue(repaired.metadata_updated)
            self.assertEqual(len(list(root.glob("*.mp3"))), 1)
            published = root / repaired.filename
            enriched = published.read_bytes()
            third = publish_mp3(temp, root, "Track.mp3", TrackMetadata("Another title", "Another artist", b"bad image"))
            self.assertFalse(third.metadata_updated)
            self.assertEqual(published.read_bytes(), enriched)
            self.assertEqual(str(ID3(published)["TPE1"]), "Artist")
            self.assertEqual(ID3(published).getall("APIC")[0].data, COVER)

    def test_completely_untagged_mp3_gets_telegram_fields_without_changing_audio(self):
        with tempfile.TemporaryDirectory() as d:
            root, temp = Path(d), Path(d) / ".incoming.part"
            shutil.copyfile(FIXTURE, temp)
            ID3(temp).delete(temp)
            self.assertIsNone(MP3(temp).tags)
            frames = audio_frames(temp)
            result = publish_mp3(temp, root, "Track.mp3", TrackMetadata("Title", "Artist", COVER))
            published = root / result.filename
            self.assertEqual(str(ID3(published)["TPE1"]), "Artist")
            self.assertEqual(ID3(published).getall("APIC")[0].data, COVER)
            self.assertEqual(audio_frames(published), frames)
            self.assertEqual(result.missing_metadata, ())

    def test_absent_metadata_is_reported_and_invalid_cover_never_replaces_a_track(self):
        with tempfile.TemporaryDirectory() as d:
            root, temp = Path(d), Path(d) / ".incoming.part"
            shutil.copyfile(FIXTURE, temp)
            tags = ID3(temp)
            tags.delall("TPE1")
            tags.save(temp)
            result = publish_mp3(temp, root, "Track.mp3", TrackMetadata())
            self.assertEqual(result.missing_metadata, ("исполнитель", "обложка"))
            previous = (root / result.filename).read_bytes()
            with self.assertRaises(IntegrationError):
                publish_mp3(temp, root, "Track.mp3", TrackMetadata(cover=b"not an image"))
            self.assertEqual((root / result.filename).read_bytes(), previous)
            self.assertEqual(list(root.glob(".shrimp-tags-*")), [])


class MusicHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ui = BotUI(Settings(allowed_users=frozenset({42}), navidrome_import_dir=str(self.root)), None)
        self.ui.show = AsyncMock()

    def update(self, *, audio=False, user=42, size=None):
        async def download(**kwargs):
            shutil.copyfile(FIXTURE, kwargs["custom_path"])

        remote = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download))
        media = SimpleNamespace(file_name="Трек.mp3", mime_type="audio/mpeg", title="Трек",
                                file_size=FIXTURE.stat().st_size if size is None else size,
                                get_file=AsyncMock(return_value=remote))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=42),
                                 effective_message=SimpleNamespace(audio=media if audio else None,
                                                                   document=None if audio else media))
        return update, media, remote

    async def test_audio_and_document_import_and_cleanup(self):
        for audio in (True, False):
            update, media, remote = self.update(audio=audio)
            await self.ui.protect(self.ui.import_music)(update, None)
            media.get_file.assert_awaited_once()
            remote.download_to_drive.assert_awaited_once()
            self.assertEqual(list(self.root.glob("*.part")), [])
        self.assertEqual(len(list(self.root.glob("*.mp3"))), 1)
        self.assertIn("уже есть", self.ui.show.await_args.args[1])

    async def test_denied_users_and_oversized_files_are_never_downloaded(self):
        for kwargs in ({"user": 99}, {"size": MAX_MP3_BYTES + 1}):
            update, media, _ = self.update(**kwargs)
            await self.ui.protect(self.ui.import_music)(update, None)
            media.get_file.assert_not_awaited()
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_failed_download_leaves_no_partial_file_or_token_in_reply(self):
        update, _, remote = self.update()
        remote.download_to_drive.side_effect = NetworkError("https://api.telegram.org/file/botSECRET")
        await self.ui.protect(self.ui.import_music)(update, None)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertNotIn("SECRET", self.ui.show.await_args.args[1])

    async def test_truncated_download_is_not_published(self):
        update, _, _ = self.update(size=FIXTURE.stat().st_size + 1)
        await self.ui.protect(self.ui.import_music)(update, None)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("не полностью", self.ui.show.await_args.args[1])

    async def test_busy_import_does_not_download_another_track(self):
        update, media, _ = self.update()
        async with self.ui.music_slots:
            await self.ui.protect(self.ui.import_music)(update, None)
        media.get_file.assert_not_awaited()

    async def test_media_handler_is_registered(self):
        app = build_application(Settings(token="123456:dummy-token"))
        try:
            self.assertTrue(any(isinstance(h, MessageHandler) for h in app.handlers[0]))
        finally:
            await app.bot_data["ui"].api.http.aclose()

    async def test_audio_performer_and_separate_thumbnail_reach_id3(self):
        update, media, _ = self.update(audio=True)
        # This download has no artist, title or artwork embedded in the MP3.
        async def download(**kwargs):
            shutil.copyfile(FIXTURE, kwargs["custom_path"])
            tags = ID3(kwargs["custom_path"])
            tags.delall("TIT2")
            tags.delall("TPE1")
            tags.save(kwargs["custom_path"])
        with tempfile.NamedTemporaryFile() as f:
            await download(custom_path=f.name)
            media.file_size = Path(f.name).stat().st_size
        media.get_file.return_value.download_to_drive.side_effect = download
        media.performer = "Telegram artist"
        image = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(COVER)))
        media.thumbnail = SimpleNamespace(file_size=len(COVER), get_file=AsyncMock(return_value=image))
        await self.ui.protect(self.ui.import_music)(update, None)
        published = next(self.root.glob("*.mp3"))
        tags = ID3(published)
        self.assertEqual(str(tags["TIT2"]), "Трек")
        self.assertEqual(str(tags["TPE1"]), "Telegram artist")
        self.assertEqual(tags.getall("APIC")[0].data, COVER)

    async def test_existing_artwork_prevents_thumbnail_download(self):
        update, media, _ = self.update(audio=True)
        async def download(**kwargs):
            shutil.copyfile(FIXTURE, kwargs["custom_path"])
            tags = ID3(kwargs["custom_path"])
            tags.add(APIC(mime="image/png", type=3, data=COVER))
            tags.save(kwargs["custom_path"])
        with tempfile.NamedTemporaryFile() as f:
            await download(custom_path=f.name)
            media.file_size = Path(f.name).stat().st_size
        media.get_file.return_value.download_to_drive.side_effect = download
        media.thumbnail = SimpleNamespace(file_size=100, get_file=AsyncMock())
        await self.ui.protect(self.ui.import_music)(update, None)
        media.thumbnail.get_file.assert_not_awaited()
        self.assertEqual(ID3(next(self.root.glob("*.mp3"))).getall("APIC")[0].data, COVER)

    async def test_thumbnail_download_failure_does_not_silently_drop_the_cover(self):
        update, media, _ = self.update(audio=True)
        media.thumbnail = SimpleNamespace(file_size=100, get_file=AsyncMock(side_effect=NetworkError("secret")))
        await self.ui.protect(self.ui.import_music)(update, None)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("обложку", self.ui.show.await_args.args[1])
