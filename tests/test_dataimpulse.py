# ABOUTME: Unit tests for Dataimpulse proxy URL construction, country
# ABOUTME: validation, credential errors, and secret redaction.
"""Dataimpulse proxy URL tests.

The URL contains a password, so these tests also assert that errors and logs
never contain credential values.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlsplit

import pytest

from hermes_steel.dataimpulse import (
    build_dataimpulse_proxy_url,
    build_dataimpulse_username,
    normalize_country,
)


@pytest.fixture(autouse=True)
def _proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_PROXY_HOST", "gw.dataimpulse.com")
    monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "823")
    monkeypatch.setenv("DATAIMPULSE_PROXY_USERNAME", "login")
    monkeypatch.setenv("DATAIMPULSE_PROXY_PASSWORD", "p@ss:w/rd?")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("de", "DE"), ("US", "US"), (" us ", "US")],
)
def test_country_codes_are_normalized_to_uppercase(given: str, expected: str) -> None:
    assert normalize_country(given) == expected


@pytest.mark.parametrize("country", ["", None, "Germany", "ZZ", "D", "12"])
def test_invalid_country_rejected(country: object) -> None:
    with pytest.raises(ValueError, match="proxy_country"):
        normalize_country(country)


@pytest.mark.parametrize(
    ("country", "marker"),
    [("DE", "__cr.de"), ("US", "__cr.us")],
)
def test_username_contains_requested_country(country: str, marker: str) -> None:
    username = build_dataimpulse_username("login", country)
    assert marker in username
    assert username.endswith(";anon.1")


def test_proxy_url_encodes_username_and_password() -> None:
    url = build_dataimpulse_proxy_url("de")
    parsed = urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.hostname == "gw.dataimpulse.com"
    assert parsed.port == 823
    assert unquote(parsed.username or "") == "login__cr.de;anon.1"
    assert unquote(parsed.password or "") == "p@ss:w/rd?"
    assert "%40" in parsed.password  # '@' must not break the authority


@pytest.mark.parametrize(
    "missing",
    [
        "DATAIMPULSE_PROXY_HOST",
        "DATAIMPULSE_PROXY_PORT",
        "DATAIMPULSE_PROXY_USERNAME",
        "DATAIMPULSE_PROXY_PASSWORD",
    ],
)
def test_missing_credentials_fail_clearly_without_values(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValueError, match=missing):
        build_dataimpulse_proxy_url("DE")


@pytest.mark.parametrize("port", ["0", "65536", "abc", "823/path"])
def test_invalid_port_rejected(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", port)
    with pytest.raises(ValueError, match="DATAIMPULSE_PROXY_PORT"):
        build_dataimpulse_proxy_url("DE")


@pytest.mark.parametrize("host", ["", "http://proxy", "host:823", "bad host"])
def test_invalid_host_rejected(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("DATAIMPULSE_PROXY_HOST", host)
    with pytest.raises(ValueError, match="DATAIMPULSE_PROXY_HOST"):
        build_dataimpulse_proxy_url("DE")


def test_password_never_appears_in_error_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "not-a-port")
    with pytest.raises(ValueError) as exc_info:
        build_dataimpulse_proxy_url("DE")
    assert "p@ss:w/rd?" not in str(exc_info.value)


def test_password_never_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "0")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError):
            build_dataimpulse_proxy_url("DE")
    assert "p@ss:w/rd?" not in caplog.text
