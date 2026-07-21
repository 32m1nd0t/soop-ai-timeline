import json
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from soop_timeline.services.live_stream import fetch_live_audio_source
from soop_timeline.services.manual_link import (
    parse_soop_link,
    resolve_vod_link,
)


class FakeResponse:
    def __init__(self, payload, final_url):
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, size):
        return self.raw[:size]


class ManualLinkTests(unittest.TestCase):
    def test_parses_vod_and_live_links(self):
        vod = parse_soop_link("https://vod.sooplive.com/player/12345?change_second=3")
        live = parse_soop_link("play.sooplive.com/sample_id/98765")
        channel_live = parse_soop_link("https://play.sooplive.com/sample_id")

        self.assertEqual((vod.kind, vod.vod_id), ("vod", "12345"))
        self.assertEqual(
            (live.kind, live.channel_id, live.broadcast_no),
            ("live", "sample_id", "98765"),
        )
        self.assertEqual(channel_live.broadcast_no, "")

    def test_rejects_non_soop_link(self):
        with self.assertRaisesRegex(ValueError, "SOOP 다시보기"):
            parse_soop_link("https://example.com/player/123")

    def test_resolves_public_vod_metadata(self):
        payload = {
            "result": 1,
            "data": {
                "is_public": 1,
                "is_paid": False,
                "is_ppv": False,
                "adult_status": "pass",
                "title": "수동 영상",
                "writer_id": "sample",
                "writer_nick": "샘플",
                "total_file_duration": 3_723_000,
                "write_tm": "2026-07-21 10:00",
                "thumb": "https://videoimg.sooplive.com/thumb.jpg",
            },
        }
        parsed = parse_soop_link("https://vod.sooplive.com/player/123")
        with patch(
            "soop_timeline.services.manual_link.urlopen",
            return_value=FakeResponse(payload, "https://api.m.sooplive.com/station/video/a/view"),
        ):
            result = resolve_vod_link(parsed)

        self.assertEqual(result.vod_id, "123")
        self.assertEqual(result.channel_id, "sample")
        self.assertEqual(result.duration_text, "01:02:03")
        self.assertEqual(result.title, "수동 영상")

    def test_live_source_uses_screen_runtime_and_low_quality_hls(self):
        live_payload = {
            "CHANNEL": {
                "RESULT": 1,
                "BNO": 98765,
                "BJID": "sample",
                "BJNICK": "샘플",
                "TITLE": "라이브 테스트",
                "BTIME": 3_600,
                "BPWD": "N",
                "BSTATUS": "BROADING",
                "GRADE": 0,
                "P_MIN_TIER": 0,
                "RMD": "https://livestream-manager.sooplive.com",
                "CDN": "lg_cdn",
            }
        }
        aid_payload = {"CHANNEL": {"RESULT": 1, "AID": "token-value"}}
        manager_payload = {
            "result": 1,
            "view_url": "https://live-pcweb-kr-cdn-z02.sooplive.com/live/auth_playlist.m3u8",
        }
        responses = [
            FakeResponse(live_payload, "https://live.sooplive.com/afreeca/player_live_api.php"),
            FakeResponse(aid_payload, "https://live.sooplive.com/afreeca/player_live_api.php"),
            FakeResponse(
                manager_payload,
                "https://livestream-manager.sooplive.com/broad_stream_assign.html",
            ),
        ]
        with patch(
            "soop_timeline.services.live_stream.urlopen",
            side_effect=responses,
        ) as mocked, patch(
            "soop_timeline.services.live_stream.time.monotonic",
            side_effect=[100.0, 102.0],
        ):
            source = fetch_live_audio_source(
                "sample",
                "98765",
                "https://play.sooplive.com/sample/98765",
                lambda: False,
            )

        manager_request = mocked.call_args_list[2].args[0]
        manager_query = parse_qs(urlparse(manager_request.full_url).query)
        stream_query = parse_qs(urlparse(source.stream_url).query)
        self.assertEqual(source.runtime_seconds, 3_602)
        self.assertEqual(source.broadcast_no, "98765")
        self.assertEqual(
            manager_query["broad_key"],
            ["98765-common-sd-hls"],
        )
        self.assertEqual(stream_query["aid"], ["token-value"])


if __name__ == "__main__":
    unittest.main()
