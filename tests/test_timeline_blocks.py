import unittest

from soop_timeline.services.timeline_blocks import block_label, split_timeline


class TimelineBlockTests(unittest.TestCase):
    def test_short_text_stays_in_one_block(self):
        self.assertEqual(split_timeline("hello", 10), ["hello"])

    def test_split_preserves_text_exactly(self):
        text = "00:00:00 첫 번째 주제\n00:10:00 두 번째 주제\n00:20:00 세 번째 주제"
        blocks = split_timeline(text, 30)
        self.assertEqual("".join(blocks), text)
        self.assertTrue(all(len(block) <= 30 for block in blocks))

    def test_prefers_line_boundary(self):
        text = "12345\n67890\n"
        self.assertEqual(split_timeline(text, 6), ["12345\n", "67890\n"])

    def test_very_long_line_is_split_as_last_resort(self):
        text = "x" * 27
        blocks = split_timeline(text, 10)
        self.assertEqual([10, 10, 7], [len(block) for block in blocks])
        self.assertEqual("".join(blocks), text)

    def test_empty_text_has_one_editable_block(self):
        self.assertEqual(split_timeline("", 10), [""])

    def test_labels_map_root_and_replies(self):
        self.assertEqual(block_label(0, 3), "댓글 · 1/3")
        self.assertEqual(block_label(2, 3), "대댓글 2 · 3/3")


if __name__ == "__main__":
    unittest.main()

