import unittest

from soop_timeline.services.channel_id import normalize_channel_id


class ChannelIdTests(unittest.TestCase):
    def test_plain_id(self):
        self.assertEqual(normalize_channel_id(" streamer_01 "), "streamer_01")

    def test_station_url(self):
        self.assertEqual(
            normalize_channel_id("https://www.sooplive.com/station/sample01/vod"),
            "sample01",
        )

    def test_legacy_channel_url(self):
        self.assertEqual(
            normalize_channel_id("https://ch.sooplive.co.kr/sample02/vods"),
            "sample02",
        )

    def test_invalid_value(self):
        with self.assertRaises(ValueError):
            normalize_channel_id("not a valid id")


if __name__ == "__main__":
    unittest.main()
