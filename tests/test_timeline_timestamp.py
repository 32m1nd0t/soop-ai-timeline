import unittest
from types import SimpleNamespace

from soop_timeline.services.timeline_timestamp import (
    adjust_timestamp_on_current_line,
    format_timestamp_seconds,
    merge_current_timeline_line_with_previous,
    parse_timestamp,
    shift_all_timestamps,
    timestamp_at_position,
)
from soop_timeline.ui.review_player import (
    ResilientQtWebView2Widget,
    SoopReviewPlayer,
    build_close_script,
    build_exit_fullscreen_script,
    build_fullscreen_escape_guard_script,
    build_player_action_script,
    build_player_url,
    build_seek_script,
    build_seek_verification_script,
)


class TimelineTimestampTests(unittest.TestCase):
    def test_adjusts_current_and_all_timeline_timestamps(self):
        text = "오늘의 콘텐츠: 테스트\n\n00:00:03 시작\n00:10:00 다음 주제\n"
        updated, changed = adjust_timestamp_on_current_line(
            text,
            text.index("시작"),
            -5,
        )
        self.assertTrue(changed)
        self.assertIn("00:00:00 시작", updated)

        shifted, count = shift_all_timestamps(updated, 10)
        self.assertEqual(count, 2)
        self.assertIn("00:00:10 시작", shifted)
        self.assertIn("00:10:10 다음 주제", shifted)

    def test_merges_current_summary_with_previous_timestamp(self):
        text = "오늘의 콘텐츠: 테스트\n\n00:01:00 꿈 이야기\n00:03:00 시청자 반응\n"
        merged, changed = merge_current_timeline_line_with_previous(
            text,
            text.index("시청자"),
        )
        self.assertTrue(changed)
        self.assertIn("00:01:00 꿈 이야기 · 시청자 반응", merged)
        self.assertNotIn("00:03:00", merged)

    def test_parses_full_and_short_timestamps(self):
        self.assertEqual(parse_timestamp("01:56:07"), 6_967)
        self.assertEqual(parse_timestamp("09:24"), 564)
        self.assertIsNone(parse_timestamp("01:60:00"))

    def test_finds_timestamp_only_when_position_is_on_it(self):
        line = "01:56:07 본격적인 게임 시작"
        hit = timestamp_at_position(line, 4)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.seconds, 6_967)
        self.assertEqual((hit.start, hit.end), (0, 8))
        self.assertIsNone(timestamp_at_position(line, 12))

    def test_finds_timestamp_after_prefix(self):
        line = "구간 02:03:53 운전면허 이야기"
        hit = timestamp_at_position(line, 8)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.text, "02:03:53")
        self.assertEqual(hit.seconds, 7_433)

    def test_formats_player_position(self):
        self.assertEqual(format_timestamp_seconds(7_433), "02:03:53")

    def test_seek_script_targets_soop_video_and_requested_time(self):
        script = build_seek_script(7_433)
        self.assertIn("const target = 7433", script)
        self.assertIn("const seekTolerance = 1.5", script)
        # The replay is chosen by media presence, not the first <video> element.
        self.assertIn("__pickVod", script)
        self.assertIn("video.currentTime = target", script)
        self.assertIn("video.muted = false", script)
        self.assertIn("VOD 보기", script)

    def test_seek_script_uses_soop_global_progress_for_multipart_vod(self):
        script = build_seek_script(18_116, 24_357)
        self.assertIn("const expectedTotal = 24357", script)
        self.assertIn("__dispatchSoopSeek(target, globalTotal)", script)
        self.assertIn("#player .progress", script)
        self.assertIn("strategy: 'soop-progress'", script)
        self.assertIn("correctedLocal = localCurrent + correction", script)
        self.assertIn("secondsPerPixel: total / rect.width", script)
        self.assertIn("__soopFineCorrectionWindow(globalTotal)", script)
        self.assertIn("Math.min(300, (total / rect.width) * 2)", script)
        self.assertIn("globalTotal > availableEnd + 15", script)

    def test_seek_script_prefers_precise_direct_seek_inside_first_part(self):
        script = build_seek_script(12_282, 24_357)
        direct_seek = script.index("strategy: 'active-part-current-time'")
        global_seek = script.index(
            "const dispatched = __dispatchSoopSeek(target, globalTotal)"
        )
        self.assertLess(direct_seek, global_seek)
        self.assertIn("firstPartIsActive && target < availableEnd - 1", script)
        self.assertIn("issued: true", script)

    def test_seek_verification_only_reads_clock(self):
        script = build_seek_verification_script(12_282)
        self.assertIn("const clocks = __readSoopClocks()", script)
        self.assertIn("const seekTolerance = 1.5", script)
        self.assertIn("strategy: 'clock-verification'", script)
        self.assertNotIn(
            "const dispatched = __dispatchSoopSeek(target, globalTotal)",
            script,
        )
        self.assertNotIn("video.currentTime", script)

    def test_seek_verification_can_issue_only_a_local_fine_correction(self):
        script = build_seek_verification_script(
            12_282,
            allow_correction=True,
        )
        self.assertIn("video.currentTime = correctedLocal", script)
        self.assertIn("correctionIssued: true", script)
        self.assertIn("strategy: 'clock-fine-correction'", script)
        self.assertIn("__soopFineCorrectionWindow(clocks.total)", script)
        self.assertNotIn(
            "const dispatched = __dispatchSoopSeek(target, globalTotal)",
            script,
        )

    def test_pending_seek_switches_retries_to_read_only_verification(self):
        class StubWebView:
            is_ready = True

            def __init__(self):
                self.script = ""

            def evaluate_js(self, script, callback):
                self.script = script

        web_view = StubWebView()
        player = SimpleNamespace(
            _dom_loaded=True,
            _pending_seconds=12_282,
            _seek_in_flight=False,
            _seek_attempts=0,
            _seek_generation=1,
            _seek_command_sent=True,
            _fine_correction_sent=True,
            _duration_seconds=24_357,
            web_view=web_view,
        )
        SoopReviewPlayer._attempt_seek(player)
        self.assertTrue(player._seek_in_flight)
        self.assertIn("strategy: 'clock-verification'", web_view.script)
        self.assertNotIn("video.currentTime", web_view.script)

    def test_pending_seek_allows_one_local_fine_correction(self):
        class StubWebView:
            is_ready = True

            def __init__(self):
                self.script = ""

            def evaluate_js(self, script, callback):
                self.script = script

        web_view = StubWebView()
        player = SimpleNamespace(
            _dom_loaded=True,
            _pending_seconds=12_282,
            _seek_in_flight=False,
            _seek_attempts=0,
            _seek_generation=1,
            _seek_command_sent=True,
            _fine_correction_sent=False,
            _duration_seconds=24_357,
            web_view=web_view,
        )
        SoopReviewPlayer._attempt_seek(player)
        self.assertTrue(player._seek_in_flight)
        self.assertIn("video.currentTime = correctedLocal", web_view.script)
        self.assertNotIn(
            "const dispatched = __dispatchSoopSeek(target, globalTotal)",
            web_view.script,
        )

    def test_initial_fine_correction_marks_both_commands_as_sent(self):
        class Recorder:
            def __init__(self):
                self.values = []

            def setText(self, value):
                self.values.append(value)

            def emit(self, value):
                self.values.append(value)

        label = Recorder()
        status = Recorder()
        player = SimpleNamespace(
            _seek_in_flight=True,
            _seek_generation=2,
            _pending_seconds=12_282,
            _seek_command_sent=False,
            _fine_correction_sent=False,
            _seek_attempts=7,
            time_label=label,
            status_changed=status,
        )
        SoopReviewPlayer._handle_seek_result(
            player,
            2,
            12_282,
            {
                "success": True,
                "result": {
                    "ok": False,
                    "reason": "target-not-ready",
                    "issued": True,
                    "correctionIssued": True,
                },
            },
        )
        self.assertTrue(player._seek_command_sent)
        self.assertTrue(player._fine_correction_sent)
        self.assertEqual(player._seek_attempts, 0)
        self.assertTrue(any("한 번 정밀 보정" in value for value in status.values))

    def test_issued_seek_is_never_sent_again_while_landing(self):
        class Recorder:
            def __init__(self):
                self.values = []

            def setText(self, value):
                self.values.append(value)

            def emit(self, value):
                self.values.append(value)

        label = Recorder()
        status = Recorder()
        player = SimpleNamespace(
            _seek_in_flight=True,
            _seek_generation=2,
            _pending_seconds=12_282,
            _seek_command_sent=False,
            _seek_attempts=7,
            time_label=label,
            status_changed=status,
        )
        SoopReviewPlayer._handle_seek_result(
            player,
            2,
            12_282,
            {
                "success": True,
                "result": {
                    "ok": False,
                    "reason": "target-not-ready",
                    "issued": True,
                },
            },
        )
        self.assertTrue(player._seek_command_sent)
        self.assertEqual(player._seek_attempts, 0)
        self.assertTrue(any("한 번" in value for value in status.values))

    def test_review_player_uses_official_embed_page(self):
        url = build_player_url("200312857").toString()
        self.assertTrue(url.startswith("https://vod.sooplive.com/player/200312857/embed?"))
        self.assertIn("autoPlay=true", url)
        self.assertIn("mutePlay=true", url)
        self.assertIn("showChat=false", url)

    def test_player_action_scripts_support_position_and_relative_seek(self):
        position_script = build_player_action_script(
            "position",
            expected_duration=24_357,
        )
        relative_script = build_player_action_script(
            "relative",
            -10,
            expected_duration=24_357,
        )
        self.assertIn("reportedCurrent", position_script)
        self.assertIn("const expectedTotal = 24357", position_script)
        self.assertIn("+ -10", relative_script)
        self.assertIn("localTarget = localCurrent + -10", relative_script)
        self.assertIn("__dispatchSoopSeek(target, globalTotal)", relative_script)

    def test_close_script_stops_and_mutes_every_video(self):
        script = build_close_script()
        self.assertIn("querySelectorAll('video')", script)
        self.assertIn("video.pause()", script)
        self.assertIn("video.muted = true", script)

    def test_close_button_hides_reusable_player_instead_of_disposing_it(self):
        calls = []

        class CloseEvent:
            def ignore(self):
                calls.append("ignored")

        player = SimpleNamespace(close_player=lambda: calls.append("hidden"))
        SoopReviewPlayer.closeEvent(player, CloseEvent())
        self.assertEqual(calls, ["ignored", "hidden"])

    def test_disposed_native_webview_is_reported_as_unhealthy(self):
        player = SimpleNamespace(
            is_ready=True,
            _webview=SimpleNamespace(IsDisposed=True),
            _webview_hwnd=123,
        )
        self.assertFalse(
            ResilientQtWebView2Widget.native_control_healthy(player)
        )

    def test_lazy_webview_is_healthy_before_native_control_exists(self):
        player = SimpleNamespace(
            is_ready=False,
            _webview=None,
            _webview_hwnd=None,
        )
        self.assertTrue(
            ResilientQtWebView2Widget.native_control_healthy(player)
        )

    def test_fullscreen_helpers_restore_both_page_and_player_window(self):
        exit_script = build_exit_fullscreen_script()
        guard_script = build_fullscreen_escape_guard_script()
        self.assertIn("document.exitFullscreen()", exit_script)
        self.assertIn("event.key !== 'Escape'", guard_script)
        self.assertIn("api.exitFullscreen()", guard_script)


if __name__ == "__main__":
    unittest.main()
