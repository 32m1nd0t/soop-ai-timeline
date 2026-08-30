import time
import unittest

from soop_timeline.services.soop_auth import (
    authenticated_headers,
    authorize_soop_resource,
    clear_soop_session_cookies,
    has_soop_session,
    revoke_soop_resource,
    soop_cookie_header,
    store_soop_session_cookies,
)


class SoopAuthenticationTests(unittest.TestCase):
    def tearDown(self):
        clear_soop_session_cookies()

    def test_only_soop_https_requests_receive_matching_cookies(self):
        count = store_soop_session_cookies(
            [
                {
                    "name": "AuthTicket",
                    "value": "secret-ticket",
                    "domain": ".sooplive.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "PlayerPref",
                    "value": "adult-ok",
                    "domain": "vod.sooplive.com",
                    "path": "/player",
                    "secure": True,
                },
                {
                    "name": "Unrelated",
                    "value": "must-not-leak",
                    "domain": ".example.com",
                    "path": "/",
                },
            ]
        )

        self.assertEqual(count, 2)
        self.assertEqual(
            soop_cookie_header("https://api.m.sooplive.com/station/video/a/view"),
            "AuthTicket=secret-ticket",
        )
        self.assertEqual(
            soop_cookie_header("https://vod.sooplive.com/player/123"),
            "PlayerPref=adult-ok; AuthTicket=secret-ticket",
        )
        self.assertEqual(soop_cookie_header("https://example.com/"), "")
        self.assertEqual(soop_cookie_header("http://vod.sooplive.com/player/123"), "")
        self.assertTrue(has_soop_session())

    def test_cookies_are_dormant_until_one_resource_is_authorized(self):
        page_url = "https://vod.sooplive.com/player/19"
        api_url = "https://api.m.sooplive.com/station/video/a/view"
        store_soop_session_cookies(
            [
                {
                    "name": "AuthTicket",
                    "value": "verified-session",
                    "domain": ".sooplive.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )

        self.assertEqual(authenticated_headers(api_url, page_url), {})
        self.assertTrue(authorize_soop_resource(page_url))
        self.assertEqual(
            authenticated_headers(api_url, page_url),
            {"Cookie": "AuthTicket=verified-session"},
        )
        self.assertEqual(
            authenticated_headers(
                api_url,
                "https://vod.sooplive.com/player/20",
            ),
            {},
        )

        revoke_soop_resource(page_url)

        self.assertEqual(authenticated_headers(api_url, page_url), {})
        self.assertFalse(has_soop_session())

    def test_live_broadcast_url_variants_share_only_the_same_channel_scope(self):
        api_url = "https://live.sooplive.com/afreeca/player_live_api.php"
        store_soop_session_cookies(
            [
                {
                    "name": "AuthTicket",
                    "value": "verified-session",
                    "domain": ".sooplive.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )

        self.assertTrue(
            authorize_soop_resource("https://play.sooplive.com/sample/123")
        )
        self.assertEqual(
            authenticated_headers(
                api_url,
                "https://play.sooplive.com/sample/456",
            ),
            {"Cookie": "AuthTicket=verified-session"},
        )
        self.assertEqual(
            authenticated_headers(
                api_url,
                "https://play.sooplive.com/another/123",
            ),
            {},
        )

        revoke_soop_resource("https://play.sooplive.com/sample")
        self.assertFalse(has_soop_session())

    def test_non_soop_page_cannot_authorize_cookie_use(self):
        store_soop_session_cookies(
            [
                {
                    "name": "AuthTicket",
                    "value": "verified-session",
                    "domain": ".sooplive.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )

        self.assertFalse(
            authorize_soop_resource("https://vod.example.com/player/19")
        )
        self.assertEqual(
            authenticated_headers(
                "https://api.m.sooplive.com/station/video/a/view",
                "https://vod.example.com/player/19",
            ),
            {},
        )

    def test_expired_and_header_injection_values_are_rejected(self):
        count = store_soop_session_cookies(
            [
                {
                    "name": "Expired",
                    "value": "old",
                    "domain": ".sooplive.com",
                    "expires": time.time() - 10,
                },
                {
                    "name": "Bad;Name",
                    "value": "value",
                    "domain": ".sooplive.com",
                },
                {
                    "name": "BadValue",
                    "value": "one;two",
                    "domain": ".sooplive.com",
                },
            ]
        )

        self.assertEqual(count, 0)
        self.assertEqual(
            authenticated_headers(
                "https://www.sooplive.com/",
                "https://vod.sooplive.com/player/19",
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
