import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import BotUI, build_application
from integrations import IntegrationError
from music import MAX_MP3_BYTES, publish_mp3, upload_name
from settings import Settings
from telegram.error import NetworkError
from telegram.ext import MessageHandler


# Generated 1-second 440 Hz sine wave, not a copyrighted recording.
FIXTURE = Path(__file__).parent / "fixtures/tone.mp3"


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
