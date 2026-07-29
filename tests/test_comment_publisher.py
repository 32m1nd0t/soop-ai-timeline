import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from soop_timeline.services.comment_publisher import (
    PublicationPlan,
    build_comment_dump_script,
    build_login_probe_script,
    build_post_reply_script,
    build_post_root_script,
    build_verify_root_script,
    root_needle,
    vod_page_url,
)
from soop_timeline.ui.comment_publisher_window import SoopCommentPublisher


class VodPageUrlTests(unittest.TestCase):
    def test_builds_watch_page_not_embed(self):
        url = vod_page_url("123456")
        self.assertEqual(url, "https://vod.sooplive.com/player/123456")
        self.assertNotIn("/embed", url)

    def test_trims_whitespace(self):
        self.assertEqual(
            vod_page_url("  99  "), "https://vod.sooplive.com/player/99"
        )

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            vod_page_url("   ")

    def test_rejects_synthetic_live_session_id(self):
        with self.assertRaises(ValueError):
            vod_page_url("live-98765-20260721")


class PublicationPlanTests(unittest.TestCase):
    def test_first_block_is_root_rest_are_replies(self):
        plan = PublicationPlan.from_blocks(["댓글", "대댓글1", "대댓글2"])
        self.assertEqual(plan.root_comment, "댓글")
        self.assertEqual(plan.replies, ("대댓글1", "대댓글2"))

    def test_single_block_has_no_replies(self):
        plan = PublicationPlan.from_blocks(["하나뿐"])
        self.assertEqual(plan.root_comment, "하나뿐")
        self.assertEqual(plan.replies, ())

    def test_empty_blocks_raise(self):
        with self.assertRaises(ValueError):
            PublicationPlan.from_blocks([])


class RootNeedleTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(root_needle("00:00:00   시작\n다음"), "00:00:00 시작 다음")

    def test_limits_length(self):
        self.assertLessEqual(len(root_needle("가" * 200)), 60)


class ScriptBuilderTests(unittest.TestCase):
    """The scripts run as async function bodies inside WebView2.evaluate_js.

    We cannot execute the DOM here, but we can guarantee the builders emit a
    single well-formed body and embed user text as a safe JS literal so a
    timeline containing quotes, newlines, or backslashes cannot break out of
    the string or inject code.
    """

    HOSTILE = 'a"b\'c`d\\e\n</script> {x}'

    def _assert_body_shape(self, script: str) -> None:
        self.assertIn("return", script)
        # Balanced braces are a cheap guard against an unterminated literal
        # swallowing the rest of the body.
        self.assertEqual(script.count("{"), script.count("}"))

    def test_login_probe_is_well_formed(self):
        script = build_login_probe_script()
        self._assert_body_shape(script)
        self.assertIn("commentClickTarget", script)
        self.assertIn('li[click-target="btn_comment"] button', script)

    def test_dump_script_respects_limit(self):
        script = build_comment_dump_script(5000)
        self._assert_body_shape(script)
        self.assertIn("5000", script)

    def test_root_script_embeds_text_safely(self):
        script = build_post_root_script(self.HOSTILE)
        self._assert_body_shape(script)
        # The exact JSON encoding of the hostile text must appear verbatim.
        self.assertIn(json.dumps(self.HOSTILE, ensure_ascii=False), script)
        # And the raw closing tag must not leak in unescaped.
        self.assertNotIn("</script>{", script)

    def test_verify_script_embeds_needle(self):
        script = build_verify_root_script('quote " here')
        self._assert_body_shape(script)
        self.assertIn(json.dumps('quote " here', ensure_ascii=False), script)

    def test_reply_script_embeds_text_and_needle(self):
        script = build_post_reply_script(self.HOSTILE, "찾을 댓글")
        self._assert_body_shape(script)
        self.assertIn(json.dumps(self.HOSTILE, ensure_ascii=False), script)
        self.assertIn(json.dumps("찾을 댓글", ensure_ascii=False), script)


class PublisherWindowLogicTests(unittest.TestCase):
    def test_background_mode_keeps_webview_normally_rendered_offscreen(self):
        calls: list[object] = []
        publisher = SimpleNamespace(
            _auto_confirmed=False,
            _background_rendering=False,
            setAttribute=lambda *args: calls.append(("attribute", args)),
            move=lambda *args: calls.append(("move", args)),
            showNormal=lambda: calls.append("show-normal"),
            lower=lambda: calls.append("lower"),
            _mute_timer=SimpleNamespace(start=lambda: calls.append("mute")),
            _loaded=True,
        )

        SoopCommentPublisher.open_in_background(publisher)

        self.assertTrue(publisher._auto_confirmed)
        self.assertTrue(publisher._background_rendering)
        self.assertIn("show-normal", calls)
        self.assertIn("lower", calls)
        self.assertTrue(
            any(
                isinstance(call, tuple)
                and call[0] == "move"
                and call[1] == (-10_000, -10_000)
                for call in calls
            )
        )

    def test_comment_target_uses_trusted_webview_click_and_rechecks(self):
        clicks: list[tuple[float, float]] = []
        publisher = SimpleNamespace(
            _comment_click_in_flight=False,
            _comment_open_attempts=0,
            web_view=SimpleNamespace(
                dispatch_page_click=lambda x, y: clicks.append((x, y)) or True
            ),
            _after_comment_tab_click=lambda: None,
        )

        with patch(
            "soop_timeline.ui.comment_publisher_window.QTimer.singleShot"
        ) as single_shot:
            opened = SoopCommentPublisher._try_open_comment_tab(
                publisher,
                {"commentClickTarget": {"x": 123.5, "y": 456.25}},
            )

        self.assertTrue(opened)
        self.assertEqual(clicks, [(123.5, 456.25)])
        self.assertTrue(publisher._comment_click_in_flight)
        self.assertEqual(publisher._comment_open_attempts, 1)
        single_shot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
