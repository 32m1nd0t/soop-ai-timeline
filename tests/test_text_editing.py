import unittest

from soop_timeline.services.text_editing import (
    find_literal_matches,
    replace_literal_all,
)


class TextEditingTests(unittest.TestCase):
    def test_find_is_literal_and_case_insensitive_by_default(self):
        text = "Gemini gemini G.mini"
        self.assertEqual(
            find_literal_matches(text, "gemini"),
            [(0, 6), (7, 13)],
        )
        self.assertEqual(
            find_literal_matches(text, "G.mini"),
            [(14, 20)],
        )

    def test_case_sensitive_find(self):
        self.assertEqual(
            find_literal_matches("와우 wow WOW", "wow", case_sensitive=True),
            [(3, 6)],
        )

    def test_replace_all_preserves_literal_replacement_text(self):
        result, count = replace_literal_all(
            "마이곰이와 마이곰이",
            "마이곰이",
            r"하치$1",
        )
        self.assertEqual(result, r"하치$1와 하치$1")
        self.assertEqual(count, 2)

    def test_empty_query_does_nothing(self):
        self.assertEqual(find_literal_matches("text", ""), [])
        self.assertEqual(replace_literal_all("text", "", "x"), ("text", 0))


if __name__ == "__main__":
    unittest.main()
