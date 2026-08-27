# ABOUTME: Handoff detection — spots pages that need a human (login, CAPTCHA,
# ABOUTME: 2FA/OTP, payment confirmation) and points the agent at the viewer.
"""Flag the moments a Steel session needs a person, not another retry.

Some steps are not automatable by design: a login wall, a "verify you're
human" challenge, a one-time code, a card confirmation. Left to itself the
agent either guesses, retries the same click, or gives up quietly — while
the one thing that would actually unblock it is already on screen in the
Steel live viewer, a link the user can open on their phone, finish the step
by hand, and hand control back.

This module inspects each ``browser_*`` result for the current Steel session
and, once per session, appends a note that names the blocker, restates the
viewer URL, and tells the agent to pause retries and ask the user.

Detection is deliberately conservative — the note costs the user attention,
so a miss is cheaper than a false alarm. Signals are read from *structural*
positions only (page title, URL path, and the accessibility snapshot's
heading / form-control / button names), never from body prose, so an article
titled "why login flows are changing" stays silent.

Invoked from :func:`hermes_steel.session_note.steel_session_note` rather than
registered as its own ``transform_tool_result`` callback: the host applies the
first string a callback returns and drops the rest, so the two notes have to
share one hook. Error results are deferred there too, so a bare navigation
timeout never burns the once-per-session note.

Disable with ``hermes config set browser.steel.handoff_detection false``
(or ``STEEL_HANDOFF_DETECTION=false``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .config import steel_setting

logger = logging.getLogger(__name__)

# Sessions already handed off this process. Same lifetime and reasoning as
# session_note's sets: few sessions per run, never pruned.
_handoff_session_ids: set[str] = set()

# Markers strong enough to fire on their own: text that essentially never
# appears outside an actual challenge, addressed to the person at the screen.
_STRONG_MARKERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "a human-verification challenge",
        (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "verify you are human",
            "verify you're human",
            "i'm not a robot",
            "i am not a robot",
            "are you a robot",
            "human verification",
            "security challenge",
        ),
    ),
    (
        "a two-factor / one-time-code step",
        (
            "two-factor",
            "two factor",
            "2fa",
            "multi-factor",
            "verification code",
            "one-time code",
            "one-time passcode",
            "one time password",
            "enter otp",
            "authenticator app",
            "confirm it's you",
            "confirm its you",
            "code we sent",
            "code we texted",
            "sms code",
            "6-digit code",
            "six-digit code",
        ),
    ),
    (
        "a payment confirmation step",
        (
            "3-d secure",
            "3d secure",
            "verified by visa",
            "confirm your payment",
            "authorize this payment",
        ),
    ),
)

# URL path segments that identify an auth flow on their own.
_AUTH_PATH_MARKERS: Tuple[str, ...] = (
    "/login",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/logon",
    "/auth",
    "/oauth",
    "/sso",
    "/session/new",
    "/2fa",
    "/mfa",
    "/otp",
    "/challenge",
    "/verify",
)

# Accessibility-tree roles whose names are page structure rather than prose.
_STRUCTURAL_ROLES = frozenset(
    {"heading", "textbox", "searchbox", "button", "dialog", "alert", "form",
     "checkbox", "combobox", "status"}
)
_FIELD_ROLES = frozenset({"textbox", "searchbox", "combobox"})

# ``- heading "Sign in" [ref=e1]`` and ``button [ref=e5]: Save`` — the two
# shapes agent-browser emits for a named node.
_QUOTED_NODE = re.compile(r'^\s*-?\s*(?P<role>[a-z]+)\s+"(?P<name>[^"]*)"')
_TRAILING_NODE = re.compile(
    r'^\s*-?\s*(?P<role>[a-z]+)\s*\[ref=[^\]]*\]:\s*(?P<name>.+)$'
)


def handoff_enabled() -> bool:
    """Whether handoff detection is on — default yes, opt-out only.

    Mirrors the other knobs' precedence (agent override → env → config); the
    default differs because this one is a safety net, not a paid feature.
    """
    value = (steel_setting("STEEL_HANDOFF_DETECTION", "handoff_detection") or "").strip()
    return value.lower() not in ("0", "false", "no", "off")


def handoff_note_for(session: Dict[str, Any], result: str) -> Optional[str]:
    """Return the once-per-session handoff note, or ``None`` to stay quiet.

    Records the session as handed off when it returns a note, so the agent is
    told once and not on every subsequent call in the same session.
    """
    session_id = str(session.get("bb_session_id") or "")
    if not session_id or session_id in _handoff_session_ids:
        return None
    if not handoff_enabled():
        return None

    reason = _handoff_reason(result)
    if reason is None:
        return None

    _handoff_session_ids.add(session_id)
    logger.info(
        "Steel handoff detected on session %s (%s)", session_id, reason,
    )

    note = (
        f"Human input is likely needed here — this page looks like {reason}."
        " Stop retrying it automatically and hand off to the user:"
    )
    viewer_url = session.get("session_viewer_url")
    if viewer_url:
        note += (
            f" ask them to open the live viewer at {viewer_url} (it works on a"
            " phone), complete the step there themselves, and tell you when"
            " they are done — then continue from where you left off in the"
            " same session."
        )
    else:
        note += (
            " ask them to complete this step and tell you when they are done,"
            " then continue from where you left off."
        )
    note += (
        " Do not guess credentials, verification codes, or payment details,"
        " and do not keep clicking while you wait."
    )
    return note


def _handoff_reason(result: str) -> Optional[str]:
    """Name the blocker on this page, or ``None`` if nothing looks blocking."""
    fields = _result_fields(result)
    if fields is None:
        return None
    title, url, snapshot, warnings, structured = fields

    # The browser tool's own verdict — it already matched the page title
    # against known block pages, so no second-guessing needed.
    if warnings:
        return "a blocked or bot-challenged page"

    structural = _structural_names(snapshot)
    haystacks = [
        title,
        _url_probe(url),
        *(name for _role, name in structural),
    ]
    if not structured:
        # No page structure to be selective with — scan the text as a whole.
        # Only the strong markers run against it, so an unparsable result
        # still catches a challenge without matching passing prose.
        haystacks.append(snapshot)
    for reason, markers in _STRONG_MARKERS:
        if any(_contains(hay, markers) for hay in haystacks):
            return reason

    return _login_reason(url, structural)


def _login_reason(url: str, structural: List[Tuple[str, str]]) -> Optional[str]:
    """Fire on a sign-in wall, stay quiet on a page with a sign-in link.

    Two signals, both structural. An auth-flow URL path means the page *is*
    the sign-in step, and a rendered password field means the page is asking
    for credentials whatever it calls itself. Sign-in *vocabulary* is
    deliberately not a signal on its own: it sits in the header of most
    content sites, and a headline like "why login flows are changing" is
    prose, not a wall.
    """
    if any(marker in _url_probe(url) for marker in _AUTH_PATH_MARKERS):
        return "a sign-in wall"
    if any(
        role in _FIELD_ROLES and "password" in name.lower()
        for role, name in structural
    ):
        return "a sign-in wall"
    return None


def _result_fields(
    result: str,
) -> Optional[Tuple[str, str, str, str, bool]]:
    """Split a tool result into (title, url, snapshot, warnings, structured).

    Browser results are JSON objects. Anything else (a plain string, a JSON
    array) carries no structure to be selective with — ``structured`` is
    False and the caller falls back to whole-text matching on the strong
    markers only, which is the conservative side to fail on.
    """
    if not isinstance(result, str) or not result.strip():
        return None
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return "", "", result, "", False
    if not isinstance(payload, dict):
        return "", "", result, "", False

    return (
        _as_text(payload.get("title")),
        _as_text(payload.get("url")),
        _as_text(payload.get("snapshot")),
        _as_text(payload.get("bot_detection_warning")),
        True,
    )


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _url_probe(url: str) -> str:
    """Return the path and query of *url*, lowercased, host excluded.

    The host is dropped so ``https://login.example.com/articles/…`` — a whole
    site served from an auth-flavoured hostname — does not read as a login
    page. A leading slash is guaranteed so ``/login`` matches path starts.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    probe = path
    if parts.query:
        probe += "?" + parts.query
    if parts.fragment:
        probe += "#" + parts.fragment
    return probe.lower()


def _structural_names(snapshot: str) -> List[Tuple[str, str]]:
    """Return (role, accessible name) for the snapshot's structural nodes.

    Body text lines are dropped: only nodes the page presents as structure or
    as something to fill in can trigger a handoff.
    """
    names: List[Tuple[str, str]] = []
    if not snapshot:
        return names
    for line in snapshot.splitlines():
        match = _QUOTED_NODE.match(line) or _TRAILING_NODE.match(line)
        if not match:
            continue
        role = match.group("role").lower()
        if role not in _STRUCTURAL_ROLES:
            continue
        names.append((role, match.group("name").strip()))
    return names


def _contains(haystack: str, markers: Iterable[str]) -> bool:
    lowered = (haystack or "").lower()
    return any(marker in lowered for marker in markers)
