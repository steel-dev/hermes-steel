# ABOUTME: Shared Steel endpoint and authentication resolution for the browser
# ABOUTME: provider and scrape tool, including canonical cloud URL detection.
"""Resolve Steel cloud versus self-hosted configuration consistently."""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.steel.dev"

try:
    from agent.secret_scope import get_secret as _get_secret
except Exception:  # older Hermes hosts have no profile-scoped secret resolver
    _get_secret = os.environ.get

# Agent-set overrides, written by steel_session_options so the agent can tune
# proxy / CAPTCHA per site. Keyed by task_id: the multiplexed gateway serves
# several agents from one process, so an unscoped setting would leak between
# them. Values are strings, same encoding as the env/config fallbacks.
_session_overrides: Dict[str, Dict[str, str]] = {}
_session_overrides_lock = threading.Lock()


def secret_value(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a credential through the host's profile-scoped secret resolver."""
    return _get_secret(name, default)


def _session_key(task_id: Optional[str]) -> str:
    return task_id or "default"


def set_session_override(
    config_name: str, value: Any, task_id: Optional[str] = None
) -> None:
    """Set (or, with ``None``, clear) an agent override for one task."""
    key = _session_key(task_id)
    with _session_overrides_lock:
        if value is None:
            bucket = _session_overrides.get(key)
            if bucket is not None:
                bucket.pop(config_name, None)
                if not bucket:
                    _session_overrides.pop(key, None)
            return
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        else:
            encoded = str(value)
        _session_overrides.setdefault(key, {})[config_name] = encoded


def get_session_override(
    config_name: str, task_id: Optional[str] = None
) -> Optional[str]:
    """Return the task's agent override for a knob, if any."""
    with _session_overrides_lock:
        return _session_overrides.get(_session_key(task_id), {}).get(config_name)


def clear_session_overrides(task_id: Optional[str] = None) -> None:
    """Drop every agent override for one task."""
    with _session_overrides_lock:
        _session_overrides.pop(_session_key(task_id), None)


def _steel_config() -> Dict[str, Any]:
    """Return the ``browser.steel`` config mapping (empty when unavailable)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        steel_cfg = (cfg.get("browser") or {}).get("steel") or {}
        return steel_cfg if isinstance(steel_cfg, dict) else {}
    except Exception:
        return {}


def _hermes_config_value(name: str) -> Optional[str]:
    """Return ``browser.steel.<name>`` from the hermes config, or None.

    Lets users configure the plugin the hermes-native way::

        hermes config set browser.steel.use_proxy true

    Fail-open: any problem reading the host config means "not configured".
    """
    value = _steel_config().get(name)
    return None if value is None else str(value)


def steel_setting(
    env_key: str, config_name: str, task_id: Optional[str] = None
) -> Optional[str]:
    """Resolve a knob by precedence: agent override → env var → config.

    The agent override (steel_session_options) wins so the model can tune a
    session for the site in front of it, but only for that task. The env var is
    a profile-scoped secret override and the historic interface; config.yaml is
    the recommended, hermes-native home for a persistent default.
    """
    override = get_session_override(config_name, task_id)
    if override is not None:
        return override
    env_value = (secret_value(env_key) or "").strip()
    if env_value:
        return env_value
    return _hermes_config_value(config_name)


def steel_api_key() -> Optional[str]:
    """Return the configured API key, treating blank values as absent.

    Deliberately env-only (``~/.hermes/.env`` or the process environment):
    secrets don't belong in config.yaml.
    """
    return (secret_value("STEEL_API_KEY") or "").strip() or None


def steel_base_url() -> str:
    """Return the configured API base URL without trailing slashes."""
    configured = (steel_setting("STEEL_BASE_URL", "base_url") or "").strip().rstrip("/")
    return configured or DEFAULT_BASE_URL


def is_default_cloud_url(base_url: Optional[str] = None) -> bool:
    """Return whether *base_url* canonically identifies Steel's cloud API."""
    candidate = steel_base_url() if base_url is None else base_url.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
        hostname = (parsed.hostname or "").rstrip(".").casefold()
    except (TypeError, ValueError):
        return False

    return bool(
        parsed.scheme.casefold() == "https"
        and hostname == "api.steel.dev"
        and port in (None, 443)
        and not parsed.path.rstrip("/")
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def custom_base_url() -> Optional[str]:
    """Return a custom self-hosted base URL, or ``None`` for Steel cloud."""
    base_url = steel_base_url()
    return None if is_default_cloud_url(base_url) else base_url


_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _validate_namespace(value: object) -> str:
    namespace = str(value or "").strip()
    if not namespace:
        return "default"
    if (
        namespace in (".", "..")
        or len(namespace) > 128
        or not _NAMESPACE_RE.fullmatch(namespace)
    ):
        raise ValueError(
            "browser.steel.credentials.namespace is invalid: use letters, "
            "digits, '_', '.', ':' or '-' (not '.' or '..')."
        )
    return namespace


def _validate_origin(value: object) -> str:
    origin = str(value or "").strip()
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"browser.steel.credentials.origins contains an invalid origin: {origin!r}"
        ) from exc
    if (
        parsed.scheme.casefold() not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/")
    ):
        raise ValueError(
            f"browser.steel.credentials.origins contains an invalid origin: "
            f"{origin!r} (expected scheme://host[:port])"
        )
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme.casefold()}://{host}{f':{port}' if port else ''}"


def steel_credentials_config() -> Optional[Dict[str, Any]]:
    """Return the session credential config, or ``None`` when disabled.

    Example::

        browser:
          steel:
            credentials:
              enabled: true
              namespace: production-agent-a
              origins:
                - https://example.com
    """
    raw = _steel_config().get("credentials")
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled")
    if not (enabled is True or str(enabled).strip().lower() in ("1", "true", "yes")):
        return None

    origins_raw = raw.get("origins")
    if not isinstance(origins_raw, list) or not origins_raw:
        raise ValueError(
            "browser.steel.credentials.origins must be a non-empty list of "
            "explicit origins when credentials are enabled."
        )
    origins = [_validate_origin(origin) for origin in origins_raw]
    return {
        "namespace": _validate_namespace(raw.get("namespace")),
        "origins": origins,
    }
