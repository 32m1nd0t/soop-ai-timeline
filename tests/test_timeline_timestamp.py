import unittest

from soop_timeline.services.timeline_timestamp import (
    adjust_timestamp_on_current_line,
    format_timestamp_seconds,
    merge_current_timeline_line_with_previous,
    parse_timestamp,
    shift_all_timestamps,
    timestamp_at_position,
)
from soop_timeline.ui.review_player import (
    build_player_action_script,
    build_player_url,
    build_seek_script,
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
        self.assertTrue(script.startswith("const target = 7433"))
        self.assertIn("const target = 7433", script)
        self.assertIn("video#video", script)
        self.assertIn("video.currentTime = target", script)
        self.assertIn("video.muted = false", script)
        self.assertIn("VOD 보기", script)

    def test_review_player_uses_official_embed_page(self):
        url = build_player_url("200312857").toString()
        self.assertTrue(url.startswith("https://vod.sooplive.com/player/200312857/embed?"))
        self.assertIn("autoPlay=true", url)
        self.assertIn("mutePlay=true", url)
        self.assertIn("showChat=false", url)

    def test_player_action_scripts_support_position_and_relative_seek(self):
        self.assertIn("currentTime", build_player_action_script("position"))
        self.assertIn("+ -10", build_player_action_script("relative", -10))


if __name__ == "__main__":
    unittest.main()
