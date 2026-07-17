# ABOUTME: Shared Steel endpoint and authentication resolution for the browser
# ABOUTME: provider and scrape tool, including canonical cloud URL detection.
"""Resolve Steel cloud versus self-hosted configuration consistently."""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.steel.dev"

# Agent-set per-process overrides (highest precedence), written by the
# steel_session_options tool so the agent can tune proxy / CAPTCHA per site.
# Process-global, like the env vars and config values it overrides; keyed by
# the same names as the browser.steel.* config keys, values are "true"/"false".
_session_overrides: dict = {}


def set_session_override(config_name: str, value: Optional[bool]) -> None:
    """Set (or, with ``None``, clear) an agent override for a knob."""
    if value is None:
        _session_overrides.pop(config_name, None)
    else:
        _session_overrides[config_name] = "true" if value else "false"


def _hermes_config_value(name: str) -> Optional[str]:
    """Return ``browser.steel.<name>`` from the hermes config, or None.

    Lets users configure the plugin the hermes-native way::

        hermes config set browser.steel.use_proxy true

    Fail-open: any problem reading the host config means "not configured".
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        steel_cfg = (cfg.get("browser") or {}).get("steel") or {}
        value = steel_cfg.get(name)
        return None if value is None else str(value)
    except Exception:
        return None


def steel_setting(env_key: str, config_name: str) -> Optional[str]:
    """Resolve a knob by precedence: agent override → env var → config.

    The agent override (steel_session_options) wins so the model can tune a
    session for the site in front of it. The env var is a process-level
    override and the historic interface; config.yaml is the recommended,
    hermes-native home for a persistent default.
    """
    if config_name in _session_overrides:
        return _session_overrides[config_name]
    env_value = (os.environ.get(env_key) or "").strip()
    if env_value:
        return env_value
    return _hermes_config_value(config_name)


def steel_api_key() -> Optional[str]:
    """Return the configured API key, treating blank values as absent.

    Deliberately env-only (``~/.hermes/.env`` or the process environment):
    secrets don't belong in config.yaml.
    """
    return (os.environ.get("STEEL_API_KEY") or "").strip() or None


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
