# ABOUTME: steel_session_options tool — lets the agent tune proxy / CAPTCHA per
# ABOUTME: session and tears down the live session so the next one applies them.
"""Agent-driven Steel session tuning.

Which anti-bot features help is site-specific: some pages work better without
a proxy, and CAPTCHA solving can slow a session that never needed it. The
agent is the one watching results, so this tool lets it set proxy / CAPTCHA
per session, then tears down the current session so the next ``browser_*``
call builds a fresh one with the chosen features.

Registration happens in :func:`hermes_steel.register` via ``ctx.register_tool``.
"""

import json
import logging
from typing import Optional

from .config import set_session_override

logger = logging.getLogger(__name__)


def steel_session_options(
    use_proxy: Optional[bool] = None,
    solve_captcha: Optional[bool] = None,
    task_id: Optional[str] = None,
) -> str:
    """Set Steel session features and reset the session so they take effect.

    Omitted knobs are left unchanged. Returns a JSON status string.
    """
    applied = {}
    if use_proxy is not None:
        set_session_override("use_proxy", bool(use_proxy))
        applied["use_proxy"] = bool(use_proxy)
    if solve_captcha is not None:
        set_session_override("solve_captcha", bool(solve_captcha))
        applied["solve_captcha"] = bool(solve_captcha)

    if not applied:
        return json.dumps({
            "error": "No options provided. Pass use_proxy and/or solve_captcha "
            "(true or false).",
        })

    # Tear down the current session so the next browser_* call rebuilds it
    # with the new features. Failure here is non-fatal — the override is set,
    # and the session will pick it up whenever it is next recreated.
    try:
        from tools import browser_tool

        browser_tool.cleanup_browser(task_id)
    except Exception as exc:
        logger.debug("Session teardown after options change failed: %s", exc)

    return json.dumps({
        "applied": applied,
        "message": (
            "Steel session features updated. The previous session was closed; "
            "call browser_navigate again to start a fresh session with these "
            "settings. Paid features (proxy, CAPTCHA solving) need a verified "
            "Steel account — if the account isn't verified the session is "
            "created without them."
        ),
    })


STEEL_SESSION_OPTIONS_SCHEMA = {
    "name": "steel_session_options",
    "description": (
        "Tune the Steel cloud browser session for the current site: toggle "
        "residential proxy and CAPTCHA solving, then start a fresh session. "
        "Use when a page blocks automated access (enable proxy/CAPTCHA), or "
        "when a site works better without them (disable to avoid slowdowns). "
        "After calling this, call browser_navigate again to apply the change. "
        "Paid features require a verified Steel account; without one the "
        "session is created without them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "use_proxy": {
                "type": "boolean",
                "description": "Route the session through a residential proxy "
                "(helps with IP/geo blocks). Omit to leave unchanged.",
            },
            "solve_captcha": {
                "type": "boolean",
                "description": "Automatically solve CAPTCHAs in the session. "
                "Omit to leave unchanged.",
            },
        },
    },
}
