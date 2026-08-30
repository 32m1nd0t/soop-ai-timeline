from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class SoopCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = True
    expires: float = 0.0


class SoopLoginRequired(RuntimeError):
    """Raised when SOOP requires a signed-in, adult-verified session."""

    def __init__(self, page_url: str, content_kind: str = "VOD"):
        self.page_url = str(page_url or "https://www.sooplive.com/")
        self.content_kind = str(content_kind or "콘텐츠")
        super().__init__(
            f"19세 {self.content_kind}입니다. SOOP 로그인과 성인 인증 후 다시 시도하세요."
        )


_COOKIE_LOCK = threading.RLock()
_SESSION_COOKIES: tuple[SoopCookie, ...] = ()
_AUTHORIZED_RESOURCES: dict[str, int] = {}


def _soop_cookie_domain(domain: str) -> bool:
    host = domain.strip().lower().lstrip(".")
    return host == "sooplive.com" or host.endswith(".sooplive.com")


def _cookie_expiry(value: object) -> float:
    try:
        expires = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return expires if math.isfinite(expires) and expires > 0 else 0.0


def store_soop_session_cookies(
    cookies: Iterable[Mapping[str, object]],
) -> int:
    """Keep WebView2 SOOP cookies in memory for authenticated media requests.

    Passwords are never exposed by WebView2. Only cookies belonging to the
    ``sooplive.com`` domain are accepted, and nothing is written by this module.
    Callers must explicitly authorize one gated resource before these cookies
    can be attached to any request.
    """

    accepted: dict[tuple[str, str, str], SoopCookie] = {}
    for raw in cookies:
        name = str(raw.get("name", "") or "").strip()
        value = str(raw.get("value", "") or "")
        domain = str(raw.get("domain", "") or "").strip().lower()
        path = str(raw.get("path", "/") or "/").strip() or "/"
        if (
            not name
            or not value
            or any(character in name for character in "\r\n;=")
            or any(character in value for character in "\r\n;")
            or not _soop_cookie_domain(domain)
            or not path.startswith("/")
        ):
            continue
        expires = _cookie_expiry(raw.get("expires"))
        if expires and expires <= time.time():
            continue
        cookie = SoopCookie(
            name=name,
            value=value,
            domain=domain,
            path=path,
            secure=bool(raw.get("secure", raw.get("isSecure", True))),
            expires=expires,
        )
        accepted[(cookie.name, cookie.domain, cookie.path)] = cookie

    with _COOKIE_LOCK:
        global _SESSION_COOKIES
        _SESSION_COOKIES = tuple(accepted.values())
        return len(_SESSION_COOKIES)


def clear_soop_session_cookies() -> None:
    with _COOKIE_LOCK:
        global _SESSION_COOKIES, _AUTHORIZED_RESOURCES
        _SESSION_COOKIES = ()
        _AUTHORIZED_RESOURCES = {}


def _resource_key(page_url: str) -> str:
    try:
        parsed = urlparse(page_url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or not _soop_cookie_domain(host):
        return ""
    if host.startswith("vod.") and len(parts) >= 2 and parts[0] == "player":
        return f"vod:{parts[1]}"
    if host.startswith("play.") and parts:
        return f"live:{parts[0].lower()}"
    return f"url:{host}{parsed.path.rstrip('/') or '/'}"


def authorize_soop_resource(page_url: str) -> bool:
    key = _resource_key(page_url)
    if not key:
        return False
    with _COOKIE_LOCK:
        if not _SESSION_COOKIES:
            return False
        _AUTHORIZED_RESOURCES[key] = _AUTHORIZED_RESOURCES.get(key, 0) + 1
    return True


def revoke_soop_resource(page_url: str) -> None:
    key = _resource_key(page_url)
    if not key:
        return
    with _COOKIE_LOCK:
        global _SESSION_COOKIES
        remaining = _AUTHORIZED_RESOURCES.get(key, 0) - 1
        if remaining > 0:
            _AUTHORIZED_RESOURCES[key] = remaining
        else:
            _AUTHORIZED_RESOURCES.pop(key, None)
        if not _AUTHORIZED_RESOURCES:
            _SESSION_COOKIES = ()


def soop_resource_authorized(page_url: str) -> bool:
    key = _resource_key(page_url)
    with _COOKIE_LOCK:
        return bool(key and _AUTHORIZED_RESOURCES.get(key, 0) > 0)


def soop_cookie_header(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if parsed.scheme != "https" or not _soop_cookie_domain(host):
        return ""

    now = time.time()
    with _COOKIE_LOCK:
        cookies = tuple(_SESSION_COOKIES)
    matching: list[SoopCookie] = []
    for cookie in cookies:
        domain = cookie.domain.lstrip(".")
        domain_match = host == domain or (
            cookie.domain.startswith(".") and host.endswith(f".{domain}")
        )
        path_match = path == cookie.path or (
            path.startswith(cookie.path)
            and (cookie.path.endswith("/") or path[len(cookie.path) :].startswith("/"))
        )
        if not domain_match or not path_match:
            continue
        if cookie.secure and parsed.scheme != "https":
            continue
        if cookie.expires and cookie.expires <= now:
            continue
        matching.append(cookie)
    matching.sort(key=lambda item: len(item.path), reverse=True)
    return "; ".join(f"{item.name}={item.value}" for item in matching)


def authenticated_headers(url: str, page_url: str) -> dict[str, str]:
    if not soop_resource_authorized(page_url):
        return {}
    header = soop_cookie_header(url)
    return {"Cookie": header} if header else {}


def has_soop_session() -> bool:
    return bool(soop_cookie_header("https://www.sooplive.com/"))


def response_requires_soop_login(payload: object) -> bool:
    """Recognize SOOP login/adult-gate failures that omit normal media data."""

    try:
        text = json.dumps(payload, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        text = str(payload).lower()
    markers = (
        "로그인",
        "성인",
        "연령",
        "19세",
        "adult",
        "age verification",
        "login required",
    )
    return any(marker in text for marker in markers)
