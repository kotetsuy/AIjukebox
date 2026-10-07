import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from program_service import GENERATING, NEXT_READY, ProgramService
from liquidsoap_client import LiquidsoapClient, LiquidsoapError


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        service = ProgramService.__new__(ProgramService)
        service.phase = GENERATING
        service.current = "current.mp3"
        service.next_track = None
        service.next_intro = None
        service._prepare_task = None
        service._playback_seen = False
        service._advancing = asyncio.Lock()
        service.selector = Mock()
        service.selector.pick_next.return_value = "next.mp3"
        service.selector.row.side_effect = lambda path: {
            "filepath": path, "title": path, "artist": "artist"
        }
        service.selector.play_stack = []
        service.client = Mock()
        service.emit = Mock()
        service.emit_intro = Mock()
        service.background_url = Mock(return_value=None)
        return service

    async def test_intro_errors_do_not_disable_next_permanently(self):
        for error in (ValueError("invalid JSON"), LookupError("speaker"),
                      OSError("cache write"), AttributeError("null content")):
            with self.subTest(error=error):
                service = self.service()
                service.build_intro = Mock(side_effect=error)
                await service._prepare_next()
                self.assertEqual(service.phase, NEXT_READY)
                self.assertIsNone(service.next_intro)
                self.assertEqual(await service.cmd_next(), "next")
                service.client.push_music.assert_called_once_with("next.mp3")
                service.client.skip.assert_called_once()
                service.client.push_intro.assert_not_called()
                await service._prepare_task

    async def test_success_keeps_intro(self):
        service = self.service()
        service.build_intro = Mock(return_value=Path("next.wav"))
        await service._prepare_next()
        self.assertEqual(service.phase, NEXT_READY)
        self.assertEqual(service.next_intro, Path("next.wav"))












    async def test_previous_track_cancels_stale_preparation(self):
        service = self.service()
        service.selector.pick_next.side_effect = ["old.mp3", "new.mp3"]
        old_started = threading.Event()
        old_release = threading.Event()
        old_finished = threading.Event()
        new_started = threading.Event()
        new_release = threading.Event()

        def build(row):
            if row["filepath"] == "old.mp3":
                old_started.set()
                old_release.wait(5)
                old_finished.set()
                return Path("old.wav")
            new_started.set()
            new_release.wait(5)
            return Path("new.wav")

        service.build_intro = build
        old_task = asyncio.create_task(service._prepare_next())
        service._prepare_task = old_task
        try:
            self.assertTrue(await asyncio.to_thread(old_started.wait, 2))
            await service._advance("previous.mp3", None,
                                   skip_current=True, push_stack=False)
            self.assertTrue(await asyncio.to_thread(new_started.wait, 2))
            old_release.set()
            self.assertTrue(await asyncio.to_thread(old_finished.wait, 2))
            await asyncio.sleep(0)
            self.assertTrue(old_task.cancelled())
            self.assertEqual(service.phase, GENERATING)
            self.assertEqual(service.next_track, "new.mp3")
            self.assertIsNone(service.next_intro)
            new_release.set()
            await service._prepare_task
            self.assertEqual(service.phase, NEXT_READY)
            self.assertEqual(service.next_intro, Path("new.wav"))
        finally:
            old_release.set()
            new_release.set()
            await asyncio.gather(old_task, service._prepare_task,
                                 return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
