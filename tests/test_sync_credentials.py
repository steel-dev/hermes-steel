# ABOUTME: Tests for the Steel credentials provisioning script — API payloads
# ABOUTME: and guaranteed secret redaction in output.
"""Credentials provisioning script tests (network faked at requests)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import FakeResponse, REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "sync-steel-credentials.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("sync_steel_credentials", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STEEL_API_KEY", "steel-key")
    monkeypatch.setenv("STEEL_CREDENTIAL_USERNAME", "admin@example.com")
    monkeypatch.setenv("STEEL_CREDENTIAL_PASSWORD", "s3cr3t-value")
    return _load_script()


def test_create_sends_values_but_prints_only_metadata(
    script, capsys: pytest.CaptureFixture[str]
) -> None:
    response = FakeResponse(
        201,
        {
            "origin": "https://example.com",
            "namespace": "production-agent-a",
            "value": {"password": "should-not-print"},
        },
    )
    with patch.object(script.requests, "request", return_value=response) as request:
        assert script.main([
            "create",
            "--origin", "https://example.com",
            "--namespace", "production-agent-a",
        ]) == 0

    assert request.call_args.args[:2] == ("POST", "https://api.steel.dev/v1/credentials")
    body = request.call_args.kwargs["json"]
    assert body["origin"] == "https://example.com"
    assert body["namespace"] == "production-agent-a"
    assert body["value"] == {
        "username": "admin@example.com",
        "password": "s3cr3t-value",
    }
    out = capsys.readouterr()
    assert "s3cr3t-value" not in out.out + out.err
    assert "should-not-print" not in out.out + out.err
    assert "production-agent-a" in out.out


def test_multiple_origins_and_namespaces_are_cartesian(
    script,
) -> None:
    response = FakeResponse(200, {"success": True})
    with patch.object(script.requests, "request", return_value=response) as request:
        assert script.main([
            "delete",
            "--origin", "https://a.example",
            "--origin", "https://b.example",
            "--namespace", "ns-1",
            "--namespace", "ns-2",
        ]) == 0
    bodies = [call.kwargs["json"] for call in request.call_args_list]
    assert {(body["origin"], body["namespace"]) for body in bodies} == {
        ("https://a.example", "ns-1"),
        ("https://b.example", "ns-1"),
        ("https://a.example", "ns-2"),
        ("https://b.example", "ns-2"),
    }


def test_list_never_prints_values(
    script, capsys: pytest.CaptureFixture[str]
) -> None:
    response = FakeResponse(
        200,
        {
            "credentials": [
                {
                    "origin": "https://example.com",
                    "namespace": "production-agent-a",
                    "value": {"password": "should-not-print"},
                }
            ]
        },
    )
    with patch.object(script.requests, "request", return_value=response):
        assert script.main(["list", "--namespace", "production-agent-a"]) == 0
    out = capsys.readouterr()
    assert "https://example.com" in out.out
    assert "should-not-print" not in out.out + out.err


def test_api_error_is_concise_and_does_not_echo_secret(
    script, capsys: pytest.CaptureFixture[str]
) -> None:
    response = FakeResponse(409, {"message": "credential already exists"})
    with patch.object(script.requests, "request", return_value=response):
        assert script.main([
            "create",
            "--origin", "https://example.com",
        ]) == 1
    err = capsys.readouterr().err
    assert "HTTP 409" in err
    assert "already exists" in err
    assert "s3cr3t-value" not in err
