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

    def test_unresolved_startup_is_not_track_end(self):
        service = self.service()
        for remaining in [-1, -1, 0, -1, 180, 179]:
            self.assertFalse(service.track_ended(remaining))
        self.assertTrue(service.track_ended(0.5))
        self.assertTrue(service.track_ended(-1))

    async def test_start_prepares_music_and_intro_before_playing(self):
        service = self.service()
        service.current = None
        service.build_intro = Mock(return_value=Path("first.wav"))
        await service.start()
        self.assertEqual(service.client.mock_calls, [
            call.pause(), call.flush_queues(), call.push_music("next.mp3"),
            call.push_intro(Path("first.wav")), call.wait_ready(with_intro=True),
            call.play(),
        ])
        self.assertFalse(service.paused)
        events = [args.args[0]["event"] for args in service.emit.call_args_list]
        self.assertLess(events.index("now_playing"), len(events) - 1)
        service.emit_intro.assert_called_once_with("next.mp3")
        await service._prepare_task

    async def test_start_failure_does_not_start_partial_playback(self):
        service = self.service()
        service.build_intro = Mock(side_effect=OSError("cache write"))
        with self.assertRaises(OSError):
            await service.start()
        self.assertTrue(service.paused)
        service.client.play.assert_not_called()
        service.client.push_music.assert_not_called()

    async def test_next_cancels_old_intro_before_starting_new_one(self):
        service = self.service()
        service.build_intro = Mock(return_value=Path("next.wav"))
        await service._prepare_next()
        service.emit.reset_mock()
        self.assertEqual(await service.cmd_next(), "next")
        self.assertEqual(service.client.mock_calls, [
            call.cancel_intro(), call.push_music("next.mp3"), call.skip(),
            call.push_intro(Path("next.wav")),
        ])
        self.assertEqual(service.emit.call_args_list[0],
                         call({"event": "intro_cancelled"}))
        service.emit_intro.assert_called_once_with("next.mp3")
        await service._prepare_task

    async def test_disabled_next_does_not_cancel_intro(self):
        service = self.service()
        await service.cmd_next()
        service.client.cancel_intro.assert_not_called()

    async def test_prev_restart_cancels_intro_without_replaying_it(self):
        service = self.service()
        service.prev_threshold = 3
        service.elapsed = AsyncMock(return_value=10)
        await service.cmd_prev()
        self.assertEqual(service.client.mock_calls, [
            call.cancel_intro(), call.push_music("current.mp3"), call.skip(),
        ])
        self.assertEqual(service.emit.call_args_list[0],
                         call({"event": "intro_cancelled"}))
        service.emit_intro.assert_not_called()

    async def test_prev_cancels_intro_before_loading_previous_intro(self):
        service = self.service()
        service.prev_threshold = 3
        service.elapsed = AsyncMock(return_value=1)
        service.selector.play_stack = ["previous.mp3"]
        intro = Mock()
        meta = Mock()
        service._intro_paths = Mock(return_value=(intro, meta))
        service.build_intro = Mock(return_value=Path("next.wav"))
        self.assertEqual(await service.cmd_prev(), "prev")
        self.assertEqual(service.client.mock_calls, [
            call.cancel_intro(), call.push_music("previous.mp3"), call.skip(),
            call.push_intro(intro),
        ])
        self.assertEqual(service.emit.call_args_list[0],
                         call({"event": "intro_cancelled"}))
        await service._prepare_task

    async def test_prev_without_history_still_cancels_intro(self):
        service = self.service()
        service.prev_threshold = 3
        service.elapsed = AsyncMock(return_value=1)
        self.assertEqual(await service.cmd_prev(), "これ以上戻れません")
        self.assertEqual(service.client.mock_calls, [call.cancel_intro()])
        service.emit.assert_called_once_with({"event": "intro_cancelled"})

    def test_cancel_intro_flushes_only_configured_dj_queue(self):
        client = LiquidsoapClient("localhost", 1234, {"dj_queue": "custom_dj"})
        client.command = Mock(return_value="OK")
        self.assertEqual(client.cancel_intro(), "OK")
        client.command.assert_called_once_with("custom_dj.reset")

    def test_ready_waits_for_both_sources(self):
        client = LiquidsoapClient("localhost", 1234)
        client.command = Mock(side_effect=["false", "true", "false", "true", "true"])
        with patch("liquidsoap_client.time.sleep"):
            client.wait_ready(with_intro=True)
        self.assertEqual(client.command.call_args_list, [
            call("music.ready"), call("music.ready"), call("dj_intro.ready"),
            call("music.ready"), call("dj_intro.ready"),
        ])

    async def test_readiness_failure_keeps_startup_paused(self):
        service = self.service()
        service.current = None
        service.build_intro = Mock(return_value=Path("first.wav"))
        service.client.wait_ready.side_effect = LiquidsoapError("not ready")
        with self.assertRaises(LiquidsoapError):
            await service.start()
        service.client.play.assert_not_called()
        service.emit_intro.assert_not_called()

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
