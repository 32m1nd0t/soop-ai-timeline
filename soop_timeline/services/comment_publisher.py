"""WebView2-driven comment/reply posting into the user's own SOOP session.

This module holds only pure logic: it builds the JavaScript snippets that the
publishing window injects into an authenticated WebView2, and it validates the
publication plan. It deliberately performs no network or GUI work so the string
builders stay unit-testable.

SOOP does not expose an official comment API to this project, so we drive the
same comment box a signed-in user would use. The selectors below were captured
from the live ``vod.sooplive.com`` comment DOM:

    - login state:   ``getLoginId()`` (id string when signed in, ``false`` when not)
                     and ``isLogin()`` (boolean)
    - comment input: ``#write-inp_comment`` — a ``contenteditable`` div, not a textarea
    - submit:        ``#tabComment .btn-basic.blue`` with a ``등록`` span,
                     disabled until the editable has text
    - hidden mirror: ``input[name="szTextarea"]`` inside the write form
    - opener/list:   ``#cmmtOpener`` toggles the panel, ``ul.cmmt-list`` holds comments

Reply DOM could not be captured offline (the test station had no comments), so
the reply path follows SOOP's reused ``write-inp`` / ``등록`` / ``답글`` component
pattern and is confirmed at runtime via ``build_comment_dump_script``.
"""

from __future__ import annotations

import json

from .publisher import PublicationPlan

__all__ = [
    "PublicationPlan",
    "vod_page_url",
    "build_login_probe_script",
    "build_comment_dump_script",
    "build_post_root_script",
    "build_verify_root_script",
    "build_post_reply_script",
    "COMMENT_INPUT_SELECTORS",
    "REPLY_INPUT_SELECTORS",
    "SUBMIT_TEXT_HINTS",
    "REPLY_TOGGLE_HINTS",
    "root_needle",
]

# The full VOD watch page (not the /embed player) is where the comment box lives.
_VOD_PAGE_TEMPLATE = "https://vod.sooplive.com/player/{vod_id}"

# --- Selector configuration -------------------------------------------------
# Ordered most specific first. The JS picks the first visible, enabled candidate.
# The leading entries match today's SOOP DOM; the trailing ones are generic
# fallbacks that keep the feature limping if SOOP renames its classes.
COMMENT_INPUT_SELECTORS: tuple[str, ...] = (
    "#write-inp_comment",
    '#tabComment .write-inp[contenteditable="true"]',
    '.cmmt_inp_wrap .write-inp[contenteditable="true"]',
    '.write_wrap .write-inp[contenteditable="true"]',
    '.write-inp[contenteditable="true"]',
    'textarea[placeholder*="댓글"]',
    'textarea[name*="comment"]',
)
# Searched inside the parent comment's element, so the reply editor's id (reused
# per comment) resolves to this comment's box.
REPLY_INPUT_SELECTORS: tuple[str, ...] = (
    "#write-inp_reply",
    '.write-inp[contenteditable="true"]',
    '.reply .write-inp[contenteditable="true"]',
    '.recomment .write-inp[contenteditable="true"]',
    'textarea[placeholder*="답글"]',
    'textarea[placeholder*="댓글"]',
)
# Text shown on the submit control. SOOP uses "등록"; the rest are fallbacks.
SUBMIT_TEXT_HINTS: tuple[str, ...] = (
    "등록",
    "작성",
    "확인",
)
# Text on the control that opens a reply box under a comment.
REPLY_TOGGLE_HINTS: tuple[str, ...] = (
    "답글",
    "대댓글",
)

# Length of the distinctive prefix used to relocate the just-posted root comment.
_NEEDLE_LENGTH = 60


def vod_page_url(vod_id: str) -> str:
    """Return the comment-bearing VOD watch page for ``vod_id``."""
    cleaned = str(vod_id).strip()
    if not cleaned:
        raise ValueError("vod_id must not be empty")
    return _VOD_PAGE_TEMPLATE.format(vod_id=cleaned)


def root_needle(text: str) -> str:
    """A short, distinctive prefix used to find a comment we just posted.

    Whitespace is collapsed so it survives the DOM's own text normalization.
    """
    collapsed = " ".join(text.split())
    return collapsed[:_NEEDLE_LENGTH]


def _js(value: object) -> str:
    """Serialize a Python value into a safe JavaScript literal."""
    return json.dumps(value, ensure_ascii=False)


# Shared helpers injected at the top of every script. Centralized so selector
# logic, visibility checks, login detection, and value setting behave identically
# across probing, posting, and verification.
_HELPERS = """
function __vis(el) {
    if (!el) { return false; }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) { return false; }
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
}
function __sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function __soopLoginId() {
    try {
        if (typeof getLoginId === 'function') {
            const id = getLoginId();
            if (id) { return String(id); }
        }
    } catch (_) {}
    return '';
}
function __loggedIn() {
    if (__soopLoginId()) { return true; }
    try {
        if (typeof isLogin === 'function') { return !!isLogin(); }
    } catch (_) {}
    return false;
}
function __ensureCommentArea() {
    const wrap = document.querySelector('#tabComment, .comment_wrap');
    if (wrap && !/\\bactive\\b/.test(wrap.className || '')) {
        const opener = document.querySelector('#cmmtOpener, .cmmt_opener');
        if (opener) { try { opener.click(); } catch (_) {} }
    }
    if (wrap && wrap.scrollIntoView) {
        try { wrap.scrollIntoView({ block: 'center' }); } catch (_) {}
    }
}
function __findInput(selectors, root) {
    const scope = root || document;
    for (const selector of selectors) {
        let nodes = [];
        try { nodes = Array.from(scope.querySelectorAll(selector)); } catch (_) { continue; }
        for (const el of nodes) {
            if (!__vis(el)) { continue; }
            if (el.disabled || el.readOnly) { continue; }
            return el;
        }
    }
    return null;
}
function __setValue(el, value) {
    el.focus();
    if (el.isContentEditable) {
        el.textContent = value;
        el.dispatchEvent(new InputEvent('input', { bubbles: true }));
        el.dispatchEvent(new Event('keyup', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        // SOOP mirrors the editable text into a hidden form field on input; set
        // it directly too in case the synthetic event misses their handler.
        let scope = el;
        for (let depth = 0; depth < 6 && scope; depth += 1) {
            let hidden = null;
            try { hidden = scope.querySelector('input[name="szTextarea"]'); } catch (_) {}
            if (hidden) { hidden.value = value; break; }
            scope = scope.parentElement;
        }
        return true;
    }
    const proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
}
function __controlText(el) {
    return ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').trim();
}
function __findSubmit(input, hints, scopeRoot) {
    const wanted = (el) => {
        if (!__vis(el)) { return false; }
        const text = __controlText(el);
        return hints.some((hint) => text.includes(hint));
    };
    let scope = scopeRoot || input;
    for (let depth = 0; depth < 8 && scope; depth += 1) {
        let nodes = [];
        try {
            nodes = Array.from(scope.querySelectorAll(
                'button, a, [role="button"], input[type="submit"], input[type="button"]'
            ));
        } catch (_) { nodes = []; }
        const hit = nodes.find(wanted);
        if (hit) { return { el: hit, disabled: !!hit.disabled, text: __controlText(hit) }; }
        scope = scope.parentElement;
    }
    return { el: null, disabled: false, text: '' };
}
function __clickSubmit(info) {
    if (!info.el) { return false; }
    if (info.el.disabled) {
        // We already populated the editable and the hidden mirror field, so a
        // still-disabled button means SOOP's own enable hook did not fire; force
        // it rather than silently doing nothing.
        try { info.el.disabled = false; } catch (_) {}
    }
    info.el.click();
    return true;
}
function __findCommentByText(needle) {
    if (!needle) { return null; }
    const wanted = needle.replace(/\\s+/g, ' ').trim();
    const nodes = Array.from(document.querySelectorAll(
        'ul.cmmt-list > li, li, article, div[class*="comment"], div[class*="cmt"]'
    ));
    // Smallest matching container wins so we land on one comment, not the list.
    let best = null;
    let bestLen = Infinity;
    for (const el of nodes) {
        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!text.includes(wanted)) { continue; }
        if (text.length < bestLen) { best = el; bestLen = text.length; }
    }
    return best;
}
""".strip()


def build_login_probe_script() -> str:
    """Report whether the SOOP session is signed in and if a comment box exists."""
    selectors = _js(list(COMMENT_INPUT_SELECTORS))
    return (
        _HELPERS
        + f"""
__ensureCommentArea();
const loginId = __soopLoginId();
const input = __findInput({selectors}, document);
return {{
    ok: true,
    url: location.href,
    title: document.title || '',
    loggedIn: __loggedIn(),
    loginId: loginId,
    hasCommentInput: !!input,
}};
"""
    ).strip()


def build_comment_dump_script(max_chars: int = 12000) -> str:
    """Return trimmed HTML of the comment area for offline selector tuning."""
    limit = max(1000, int(max_chars))
    return (
        _HELPERS
        + f"""
__ensureCommentArea();
const target = document.querySelector('#tabComment')
    || document.querySelector('.comment_wrap')
    || document.querySelector('[class*="comment"]');
const html = target ? target.outerHTML : (document.body ? document.body.outerHTML : '');
return {{
    ok: !!target,
    url: location.href,
    loginId: __soopLoginId(),
    commentCount: document.querySelectorAll('ul.cmmt-list > li').length,
    html: html.slice(0, {limit}),
    truncated: html.length > {limit},
}};
"""
    ).strip()


def build_post_root_script(text: str) -> str:
    """Fill the top-level comment box with ``text`` and submit it."""
    selectors = _js(list(COMMENT_INPUT_SELECTORS))
    hints = _js(list(SUBMIT_TEXT_HINTS))
    value = _js(text)
    return (
        _HELPERS
        + f"""
if (!__loggedIn()) {{ return {{ ok: false, stage: 'not-logged-in' }}; }}
__ensureCommentArea();
const input = __findInput({selectors}, document);
if (!input) {{ return {{ ok: false, stage: 'find-input' }}; }}
__setValue(input, {value});
await __sleep(200);
const submit = __findSubmit(input, {hints}, null);
if (!submit.el) {{ return {{ ok: false, stage: 'find-submit', filled: true }}; }}
__clickSubmit(submit);
return {{ ok: true, stage: 'submitted', filled: true, wasDisabled: submit.disabled, submitText: submit.text }};
"""
    ).strip()


def build_verify_root_script(needle: str) -> str:
    """Confirm a comment containing ``needle`` is now present on the page."""
    wanted = _js(needle)
    return (
        _HELPERS
        + f"""
const el = __findCommentByText({wanted});
return {{ ok: !!el, found: !!el, commentCount: document.querySelectorAll('ul.cmmt-list > li').length }};
"""
    ).strip()


def build_post_reply_script(text: str, needle: str) -> str:
    """Open the reply box under the comment matching ``needle`` and submit ``text``."""
    wanted = _js(needle)
    reply_selectors = _js(list(REPLY_INPUT_SELECTORS))
    reply_hints = _js(list(REPLY_TOGGLE_HINTS))
    submit_hints = _js(list(SUBMIT_TEXT_HINTS))
    value = _js(text)
    return (
        _HELPERS
        + f"""
if (!__loggedIn()) {{ return {{ ok: false, stage: 'not-logged-in' }}; }}
const parent = __findCommentByText({wanted});
if (!parent) {{ return {{ ok: false, stage: 'find-parent' }}; }}
parent.scrollIntoView({{ block: 'center' }});
// Open the reply editor if it is behind a toggle.
let replyInput = __findInput({reply_selectors}, parent);
if (!replyInput) {{
    const toggle = __findSubmit(parent, {reply_hints}, parent);
    if (toggle.el) {{ toggle.el.click(); }}
    await __sleep(350);
    replyInput = __findInput({reply_selectors}, parent) || __findInput({reply_selectors}, document);
}}
if (!replyInput) {{ return {{ ok: false, stage: 'find-reply-input' }}; }}
__setValue(replyInput, {value});
await __sleep(200);
const scope = parent.contains(replyInput) ? parent : replyInput;
const submit = __findSubmit(replyInput, {submit_hints}, scope);
if (!submit.el) {{ return {{ ok: false, stage: 'find-reply-submit', filled: true }}; }}
__clickSubmit(submit);
return {{ ok: true, stage: 'submitted', filled: true, wasDisabled: submit.disabled, submitText: submit.text }};
"""
    ).strip()
