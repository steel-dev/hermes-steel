# ABOUTME: SteelBrowserProvider — Steel (steel.dev) cloud browser backend for
# ABOUTME: Hermes Agent, implementing the agent.browser_provider.BrowserProvider ABC.
"""Steel cloud browser provider.

Subclasses :class:`agent.browser_provider.BrowserProvider` (the plugin-facing
ABC from hermes-agent), mirroring the bundled ``plugins/browser/browserbase/``
and ``plugins/browser/firecrawl/`` providers.

Steel exposes a standard CDP websocket per session, so the provider hands the
dispatcher a ``cdp_url`` and the provider-agnostic wrapper in
:mod:`tools.browser_tool` drives it via ``agent-browser --cdp`` — no Steel CLI
required for the browser path. (The standalone ``steel_scrape`` tool is a
separate HTTP-only tool and is unaffected.)

One Steel quirk is normalized here: the session's ``websocketUrl`` comes back
without a path segment (``wss://connect.steel.dev?sessionId=...``), which makes
some CDP clients build a malformed request-line and get ``400 Bad Request`` on
the upgrade. We insert the ``/`` before the query and, for Steel cloud, append
the API key so the URL is
``wss://connect.steel.dev/?sessionId=...&apiKey=...``.

Config keys this provider responds to::

    browser:
      cloud_provider: "steel"
      steel:
        credentials:
          enabled: true
          namespace: production-agent-a
          origins: ["https://example.com"]

Cloud auth env vars::

    STEEL_API_KEY=...            # required for https://api.steel.dev

Optional feature knobs::

    STEEL_BASE_URL=...           # default https://api.steel.dev; self-hosted may omit key
    STEEL_USE_PROXY=true         # default false — Dataimpulse residential proxy
    STEEL_PROXY_COUNTRY=DE       # ISO 3166-1 alpha-2; required when proxy is on
    STEEL_SOLVE_CAPTCHA=true     # default false — CAPTCHA solving
    STEEL_SESSION_TIMEOUT=...     # milliseconds, integer; default 1800000
                                  # (30 min — Steel's own 5-min default kills
                                  # long agent tasks mid-flight)

Dataimpulse proxy credentials are read from ``DATAIMPULSE_PROXY_HOST``,
``DATAIMPULSE_PROXY_PORT``, ``DATAIMPULSE_PROXY_USERNAME`` and
``DATAIMPULSE_PROXY_PASSWORD``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from agent.browser_provider import BrowserProvider
from .config import (
    custom_base_url,
    is_default_cloud_url,
    steel_api_key,
    steel_base_url,
    steel_credentials_config,
    steel_setting,
)
from .dataimpulse import build_dataimpulse_proxy_url

logger = logging.getLogger(__name__)

# Session lifetime defaults, applied when STEEL_SESSION_TIMEOUT is unset.
# Steel's own default hard cap is 5 minutes — too short for agent tasks.
_DEFAULT_SESSION_TIMEOUT_MS = 1_800_000  # 30 min hard cap
_DEFAULT_INACTIVITY_TIMEOUT_MS = 600_000  # 10 min without CDP activity

_RELEASE_ATTEMPTS = 3
_RELEASE_BACKOFF_SECONDS = 0.1


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _normalize_cdp_url(ws_url: str, api_key: Optional[str] = None) -> str:
    """Return a CDP-connectable websocket URL for *ws_url*.

    Ensures a ``/`` path segment (Steel's ``websocketUrl`` omits it, which
    trips up CDP clients during the WS upgrade). Cloud credentials are appended
    as ``apiKey``; unauthenticated self-hosted endpoints omit that parameter.
    """
    if not isinstance(ws_url, str):
        raise ValueError("Steel websocketUrl must be a string")

    try:
        parts = urlsplit(ws_url)
    except ValueError as exc:
        raise ValueError(f"Invalid Steel websocketUrl: {ws_url!r}") from exc
    if parts.scheme not in {"ws", "wss"} or not parts.netloc:
        raise ValueError(f"Invalid Steel websocketUrl: {ws_url!r}")

    path = parts.path or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "apiKey"
    ]
    if api_key:
        query.append(("apiKey", api_key))
    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(query), parts.fragment)
    )


def _api_error_message(response: requests.Response) -> str:
    """Return the human-readable message from a Steel error response."""
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
    except Exception:
        pass
    return (response.text or "").strip()[:200]


def _websocket_session_ids(ws_url: object) -> set[str]:
    """Return normalized ``sessionId`` query values from a valid websocket URL."""
    if not isinstance(ws_url, str):
        return set()
    try:
        parts = urlsplit(ws_url)
    except ValueError:
        return set()
    if parts.scheme not in {"ws", "wss"} or not parts.netloc:
        return set()
    return {
        value.strip()
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key == "sessionId"
    }


class SteelBrowserProvider(BrowserProvider):
    """Steel (https://steel.dev) cloud browser backend.

    Headless browser sessions with optional residential proxy and CAPTCHA
    solving. Explicit-only: activated via ``browser.cloud_provider: steel``;
    never auto-selected (not in the registry's legacy preference walk).
    """

    @property
    def name(self) -> str:
        return "steel"

    @property
    def display_name(self) -> str:
        return "Steel"

    def is_available(self) -> bool:
        return bool(steel_api_key() or custom_base_url())

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    def _api_key(self) -> Optional[str]:
        api_key = steel_api_key()
        if api_key:
            return api_key
        if is_default_cloud_url():
            raise ValueError(
                "Steel cloud requires the STEEL_API_KEY environment variable. "
                "Get your key at https://steel.dev, or configure STEEL_BASE_URL "
                "for a self-hosted Steel instance."
            )
        return None

    def _headers(self, api_key: Optional[str]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Steel-Api-Key"] = api_key
        return headers

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, task_id: str) -> Dict[str, object]:
        api_key = self._api_key()
        base_url = steel_base_url()

        # Derive the local agent-browser session name before creating a remote
        # session.  This keeps even malformed task identifiers from creating a
        # billable session that cannot be tracked and released locally.
        task_bytes = str(task_id).encode("utf-8", errors="surrogatepass")
        task_digest = hashlib.sha256(task_bytes).hexdigest()[:12]
        session_name = f"hermes_{task_digest}_{uuid.uuid4().hex[:8]}"

        requested_proxy = _truthy(
            steel_setting("STEEL_USE_PROXY", "use_proxy", task_id)
        )
        requested_captcha = _truthy(
            steel_setting("STEEL_SOLVE_CAPTCHA", "solve_captcha", task_id)
        )
        # enable_* track the final request state (CAPTCHA may be stripped on a
        # 402 retry); requested_* preserve the original intent.
        enable_proxy, enable_captcha = requested_proxy, requested_captcha

        session_config: Dict[str, object] = {}
        if enable_proxy:
            # An explicit proxy request must fail loudly: never retry without
            # it or let the session fall back to direct browsing.
            session_config["proxyUrl"] = build_dataimpulse_proxy_url(
                steel_setting("STEEL_PROXY_COUNTRY", "proxy_country", task_id) or ""
            )
        if enable_captcha:
            session_config["solveCaptcha"] = True

        credentials = steel_credentials_config()
        if credentials:
            session_config["namespace"] = credentials["namespace"]
            session_config["credentials"] = {"origins": credentials["origins"]}

        custom_timeout_ms = steel_setting(
            "STEEL_SESSION_TIMEOUT", "session_timeout", task_id
        )
        custom_timeout = False
        if custom_timeout_ms:
            try:
                timeout_val = int(custom_timeout_ms)
                if timeout_val > 0:
                    session_config["timeout"] = timeout_val
                    custom_timeout = True
            except ValueError:
                logger.warning(
                    "Invalid STEEL_SESSION_TIMEOUT value: %s", custom_timeout_ms
                )
        if not custom_timeout:
            # Steel's API default is 300000 ms (5 minutes), which hard-kills
            # the session mid-task for any longer agent run — the CDP socket
            # dies and every subsequent browser tool call fails. Request an
            # agent-sized hard cap instead, with a server-side inactivity
            # timeout as crash insurance: hermes releases sessions itself on
            # cleanup (typically after ~2 min idle), so the inactivity cap
            # only matters when the hermes process dies without cleanup.
            session_config["timeout"] = _DEFAULT_SESSION_TIMEOUT_MS
            session_config["inactivityTimeout"] = _DEFAULT_INACTIVITY_TIMEOUT_MS

        def _post_session(config: Dict[str, object]) -> requests.Response:
            try:
                return requests.post(
                    f"{base_url}/v1/sessions",
                    headers=self._headers(api_key),
                    json=config,
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"Steel API connection failed: {exc}") from exc

        response = _post_session(session_config)

        # Free-tier degradation: Steel answers 402 when the account's plan
        # doesn't include CAPTCHA solving. Strip CAPTCHA only and retry once.
        # A requested custom proxy is never removed: the agent asked for it
        # explicitly, so a failure must surface instead of browsing directly.
        degraded_features: list = []
        degradation_reason = ""
        if response.status_code == 402 and enable_captcha:
            degradation_reason = _api_error_message(response)
            degraded_features = ["captcha_solving"]
            session_config.pop("solveCaptcha", None)
            enable_captcha = False
            logger.warning(
                "Steel rejected CAPTCHA solving (402: %s) — retrying session "
                "create without it",
                degradation_reason,
            )
            response = _post_session(session_config)

        if not response.ok:
            if response.status_code == 402 and requested_proxy:
                raise RuntimeError(
                    "Steel rejected the Dataimpulse proxy session (HTTP 402). "
                    "The proxy was not removed, so no direct session was "
                    "created. Check the Steel account plan or proxy settings."
                )
            message = (
                f"Failed to create Steel session: "
                f"{response.status_code} {response.text}"
            )
            if response.status_code == 402:
                message += (
                    " — this feature requires a verified/paid Steel plan; "
                    "see https://docs.steel.dev/overview/pricinglimits"
                )
            raise RuntimeError(message)

        try:
            session_data = response.json()
        except ValueError as exc:
            raise RuntimeError("Steel session response was not valid JSON") from exc
        if not isinstance(session_data, dict):
            raise RuntimeError(
                f"Steel session response was not an object: {session_data!r}"
            )

        raw_session_id = session_data.get("id")
        websocket_url = session_data.get("websocketUrl")

        websocket_session_ids = _websocket_session_ids(websocket_url)
        cloud_session_id = (
            raw_session_id.strip() if isinstance(raw_session_id, str) else ""
        )
        if not cloud_session_id:
            recoverable_session_ids = websocket_session_ids - {""}
            if len(recoverable_session_ids) == 1:
                recovered_session_id = next(iter(recoverable_session_ids))
                self._release_session(recovered_session_id, timeout=5)
            raise RuntimeError(
                f"Steel session response contained a missing or invalid id: "
                f"{raw_session_id!r}"
            )

        if not websocket_url:
            self._release_session(cloud_session_id, timeout=5)
            raise RuntimeError(
                f"Steel session response missing websocketUrl: {session_data!r}"
            )

        if websocket_session_ids and websocket_session_ids != {cloud_session_id}:
            self._release_session(cloud_session_id, timeout=5)
            raise RuntimeError(
                "Steel session response id did not match websocketUrl sessionId: "
                f"{cloud_session_id!r}"
            )

        try:
            cdp_url = _normalize_cdp_url(websocket_url, api_key)
        except Exception as exc:
            self._release_session(cloud_session_id, timeout=5)
            raise RuntimeError(
                f"Steel session response contained an invalid websocketUrl: "
                f"{websocket_url!r}"
            ) from exc

        # A custom proxyUrl is accepted as requested; connection failures
        # surface during navigation, not as a silent direct session. CAPTCHA
        # state is reported from the response so a silent downgrade is visible.
        proxy_active = requested_proxy
        if "solveCaptcha" in session_data:
            captcha_active = bool(session_data.get("solveCaptcha"))
        else:
            captcha_active = enable_captcha

        if requested_captcha and not captcha_active:
            if "captcha_solving" not in degraded_features:
                degraded_features.append("captcha_solving")
            if not degradation_reason:
                degradation_reason = (
                    "Steel created the session without CAPTCHA solving — the "
                    "account's plan may not include it."
                )

        features_enabled = {
            "proxy": proxy_active,
            "captcha_solving": captcha_active,
            "custom_timeout": custom_timeout,
        }

        viewer_url = session_data.get("sessionViewerUrl")
        if viewer_url:
            logger.info("Steel session %s — live viewer: %s", session_name, viewer_url)

        feature_str = ", ".join(k for k, v in features_enabled.items() if v) or "none"
        logger.info(
            "Created Steel session %s with features: %s", session_name, feature_str
        )

        result: Dict[str, object] = {
            "session_name": session_name,
            "bb_session_id": cloud_session_id,
            "cdp_url": cdp_url,
            "features": features_enabled,
        }
        if viewer_url:
            result["session_viewer_url"] = viewer_url
        if degraded_features:
            result["degraded_features"] = degraded_features
            result["degradation_reason"] = degradation_reason
        return result

    def _release_session(self, session_id: str, *, timeout: float) -> bool:
        try:
            api_key = self._api_key()
        except ValueError:
            logger.warning(
                "Cannot release Steel session %s — Steel is not configured",
                session_id,
            )
            return False

        encoded_session_id = quote(session_id, safe="")
        release_url = (
            f"{steel_base_url()}/v1/sessions/{encoded_session_id}/release"
        )
        for attempt in range(1, _RELEASE_ATTEMPTS + 1):
            try:
                response = requests.post(
                    release_url,
                    headers=self._headers(api_key),
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Steel session %s release attempt %s/%s failed: %s",
                    session_id,
                    attempt,
                    _RELEASE_ATTEMPTS,
                    exc,
                )
            except Exception as exc:
                logger.error(
                    "Exception releasing Steel session %s: %s", session_id, exc
                )
                return False
            else:
                if response.status_code in {200, 201, 204, 404, 410}:
                    logger.debug("Successfully released Steel session %s", session_id)
                    return True

                if response.status_code not in {408, 429} and not (
                    500 <= response.status_code < 600
                ):
                    logger.warning(
                        "Failed to release Steel session %s: HTTP %s - %s",
                        session_id,
                        response.status_code,
                        response.text[:200],
                    )
                    return False
                logger.warning(
                    "Steel session %s release attempt %s/%s returned HTTP %s",
                    session_id,
                    attempt,
                    _RELEASE_ATTEMPTS,
                    response.status_code,
                )

            if attempt < _RELEASE_ATTEMPTS:
                time.sleep(_RELEASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

        logger.warning(
            "Failed to release Steel session %s after %s attempts",
            session_id,
            _RELEASE_ATTEMPTS,
        )
        return False

    def close_session(self, session_id: str) -> bool:
        try:
            return self._release_session(session_id, timeout=10)
        except Exception as exc:
            logger.error("Exception closing Steel session %s: %s", session_id, exc)
            return False

    def emergency_cleanup(self, session_id: str) -> None:
        try:
            self._release_session(session_id, timeout=5)
        except Exception as exc:
            logger.debug(
                "Emergency cleanup failed for Steel session %s: %s", session_id, exc
            )

    def get_setup_schema(self) -> Dict[str, Any]:
        env_vars = []
        if not custom_base_url():
            env_vars.append(
                {
                    "key": "STEEL_API_KEY",
                    "prompt": "Steel API key",
                    "url": "https://steel.dev",
                }
            )
        return {
            "name": "Steel",
            "badge": "paid",
            "tag": "Cloud browser with proxies and CAPTCHA solving",
            "env_vars": env_vars,
            "post_setup": "agent_browser",
        }
