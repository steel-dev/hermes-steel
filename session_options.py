# ABOUTME: steel_session_options tool — lets the agent tune proxy / country /
# ABOUTME: CAPTCHA per task and tears down the live session to apply them.
"""Agent-driven Steel session tuning.

Which anti-bot features help is site-specific: some pages work better without
a proxy, and CAPTCHA solving can slow a session that never needed it. The
agent is the one watching results, so this tool lets it set proxy, country and
CAPTCHA per task, then tears down the current session so the next ``browser_*``
call builds a fresh one with the chosen features.

Proxy settings are stored per task (Hermes' browser session key), so concurrent
agents in a multiplexed gateway never inherit each other's choices.

Registration happens in :func:`hermes_steel.register` via ``ctx.register_tool``.
"""

import json
import logging
from typing import Optional

from .config import set_session_override
from .dataimpulse import normalize_country

logger = logging.getLogger(__name__)


def _error(message: str) -> str:
    return json.dumps({"error": message})


def steel_session_options(
    use_proxy: Optional[bool] = None,
    proxy_country: Optional[str] = None,
    solve_captcha: Optional[bool] = None,
    task_id: Optional[str] = None,
) -> str:
    """Set Steel session features and reset the session so they take effect.

    Omitted knobs are left unchanged. Returns a JSON status string.
    """
    country = None
    if proxy_country is not None and str(proxy_country).strip():
        try:
            country = normalize_country(proxy_country)
        except ValueError as exc:
            return _error(str(exc))
    if use_proxy is True and country is None:
        return _error(
            "proxy_country is required when enabling the Dataimpulse proxy "
            "(ISO 3166-1 alpha-2 code, for example \"DE\" or \"US\")."
        )

    applied = {}
    if use_proxy is not None:
        set_session_override("use_proxy", bool(use_proxy), task_id)
        applied["use_proxy"] = bool(use_proxy)
        if use_proxy:
            set_session_override("proxy_country", country, task_id)
            applied["proxy_country"] = country
        else:
            set_session_override("proxy_country", None, task_id)
    elif country is not None:
        set_session_override("proxy_country", country, task_id)
        applied["proxy_country"] = country
    if solve_captcha is not None:
        set_session_override("solve_captcha", bool(solve_captcha), task_id)
        applied["solve_captcha"] = bool(solve_captcha)

    if not applied:
        return _error(
            "No options provided. Pass use_proxy (with proxy_country) and/or "
            "solve_captcha."
        )

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
            "Steel session features updated for this task. The previous "
            "session was closed; call browser_navigate again to start a fresh "
            "session with these settings."
        ),
    })


STEEL_SESSION_OPTIONS_SCHEMA = {
    "name": "steel_session_options",
    "description": (
        "Tune the Steel cloud browser session for the current task: enable or "
        "disable the Dataimpulse residential proxy, choose its country, and "
        "toggle CAPTCHA solving. Proxy is disabled by default; when enabling "
        "it, proxy_country is required (ISO 3166-1 alpha-2, e.g. DE or US). "
        "Settings apply only to this task's next session. Changing them closes "
        "the current browser session, so call browser_navigate again afterwards."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "use_proxy": {
                "type": "boolean",
                "description": "Enable the Dataimpulse residential proxy for "
                "this task's next session. Omit to leave unchanged.",
            },
            "proxy_country": {
                "type": "string",
                "description": "ISO 3166-1 alpha-2 country code for the proxy "
                "(e.g. \"DE\"). Required when use_proxy is true; optional when "
                "disabling.",
            },
            "solve_captcha": {
                "type": "boolean",
                "description": "Automatically solve CAPTCHAs in the session. "
                "Omit to leave unchanged.",
            },
        },
    },
}
