# ABOUTME: Unit tests for SteelBrowserProvider — CDP URL normalization, session
# ABOUTME: lifecycle request shaping, and explicit-only registry resolution.
"""Tests for the Steel cloud browser provider.

Network calls are faked at the ``requests.post`` boundary; everything else
(URL normalization, metadata contract, host registry resolution) is real.
"""

from __future__ import annotations

import hashlib
import re
from unittest.mock import patch

import pytest

from conftest import FakeResponse
from hermes_steel import provider as steel_module
from hermes_steel.provider import SteelBrowserProvider, _normalize_cdp_url


# ---------------------------------------------------------------------------
# _normalize_cdp_url — the slash fix
# ---------------------------------------------------------------------------


class TestNormalizeCdpUrl:
    def test_inserts_slash_before_query_and_appends_api_key(self) -> None:
        url = _normalize_cdp_url("wss://connect.steel.dev?sessionId=abc", "secret")
        assert url == "wss://connect.steel.dev/?sessionId=abc&apiKey=secret"

    def test_preserves_existing_path(self) -> None:
        url = _normalize_cdp_url("wss://connect.steel.dev/cdp?sessionId=abc", "secret")
        assert url == "wss://connect.steel.dev/cdp?sessionId=abc&apiKey=secret"

    def test_handles_self_hosted_host(self) -> None:
        url = _normalize_cdp_url("wss://steel.internal.example?sessionId=xyz", "k")
        assert url == "wss://steel.internal.example/?sessionId=xyz&apiKey=k"

    def test_omits_api_key_for_unauthenticated_self_hosted_endpoint(self) -> None:
        url = _normalize_cdp_url(
            "ws://steel.internal.example?sessionId=xyz&apiKey=stale", None
        )
        assert url == "ws://steel.internal.example/?sessionId=xyz"

    @pytest.mark.parametrize("url", ["not-a-url", "https://example.com", "wss://["])
    def test_rejects_invalid_websocket_url(self, url: str) -> None:
        with pytest.raises(ValueError, match="websocketUrl"):
            _normalize_cdp_url(url, "secret")


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_false_without_api_key(self) -> None:
        assert SteelBrowserProvider().is_available() is False

    def test_true_with_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "k")
        assert SteelBrowserProvider().is_available() is True

    def test_true_with_custom_base_url_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal.example")
        assert SteelBrowserProvider().is_available() is True

    @pytest.mark.parametrize("base_url", ["   ", "https://api.steel.dev/"])
    def test_default_cloud_still_requires_api_key(
        self, monkeypatch: pytest.MonkeyPatch, base_url: str
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", base_url)
        provider = SteelBrowserProvider()
        assert provider.is_available() is False
        with pytest.raises(ValueError, match="STEEL_API_KEY"):
            provider.create_session("task-default-cloud")


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


def _ok_session_payload() -> dict:
    return {
        "id": "sess-123",
        "websocketUrl": "wss://connect.steel.dev?sessionId=sess-123",
        "sessionViewerUrl": "https://app.steel.dev/sessions/sess-123",
    }


class TestCreateSession:
    def test_returns_normalized_cdp_url_and_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            session = SteelBrowserProvider().create_session("task-1")

        assert session["bb_session_id"] == "sess-123"
        assert session["cdp_url"] == (
            "wss://connect.steel.dev/?sessionId=sess-123&apiKey=secret"
        )
        task_digest = hashlib.sha256(b"task-1").hexdigest()[:12]
        assert session["session_name"].startswith(f"hermes_{task_digest}_")
        assert re.fullmatch(r"hermes_[0-9a-f]{12}_[0-9a-f]{8}", session["session_name"])
        assert session["session_viewer_url"].endswith("sess-123")
        # No optional knobs set → all features off.
        assert session["features"] == {
            "proxy": False,
            "captcha_solving": False,
            "custom_timeout": False,
        }
        # API key travels in the Steel-Api-Key header.
        assert post.call_args.kwargs["headers"]["Steel-Api-Key"] == "secret"

    @pytest.mark.parametrize(
        "task_id",
        [
            "",
            "task/../../escape",
            "windows:task",
            "tâche-日本語",
            "bad-surrogate-\ud800",
            "x" * 256,
        ],
    )
    def test_session_name_is_bounded_and_filesystem_safe(
        self, monkeypatch: pytest.MonkeyPatch, task_id: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ):
            session = SteelBrowserProvider().create_session(task_id)

        name = session["session_name"]
        digest = hashlib.sha256(
            task_id.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:12]
        assert name.startswith(f"hermes_{digest}_")
        assert re.fullmatch(r"hermes_[0-9a-f]{12}_[0-9a-f]{8}", name)
        assert len(name) == 28

    def test_self_hosted_request_omits_auth_when_api_key_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal.example/")
        payload = {
            "id": "self-hosted-1",
            "websocketUrl": "ws://steel.internal.example?sessionId=self-hosted-1",
        }
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, payload),
        ) as post:
            session = SteelBrowserProvider().create_session("task-self-hosted")

        assert post.call_args.args[0] == (
            "http://steel.internal.example/v1/sessions"
        )
        assert post.call_args.kwargs["headers"] == {
            "Content-Type": "application/json"
        }
        assert session["cdp_url"] == (
            "ws://steel.internal.example/?sessionId=self-hosted-1"
        )

    def test_blank_base_url_falls_back_to_default_cloud_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_BASE_URL", "   ")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            SteelBrowserProvider().create_session("task-default-cloud")

        assert post.call_args.args[0] == "https://api.steel.dev/v1/sessions"

    def test_402_with_captcha_degrades_instead_of_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Free-tier accounts must work out of the box: when Steel rejects
        CAPTCHA solving with 402, retry without it and record the
        degradation so the session note can tell the user how to unlock."""
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "true")
        rejection = FakeResponse(
            402,
            {
                "error": "Payment Required",
                "message": "CAPTCHA solving is not available on the hobby plan.",
            },
        )
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[rejection, FakeResponse(200, _ok_session_payload())],
        ) as post:
            session = SteelBrowserProvider().create_session("task-degrade")

        retry_body = post.call_args_list[1].kwargs["json"]
        assert "solveCaptcha" not in retry_body
        assert session["features"]["proxy"] is False
        assert session["features"]["captcha_solving"] is False
        assert session["degraded_features"] == ["captcha_solving"]
        assert "hobby plan" in session["degradation_reason"]

    def test_402_degradation_reports_only_requested_captcha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "1")
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[
                FakeResponse(402, {"message": "not on your plan"}),
                FakeResponse(200, _ok_session_payload()),
            ],
        ):
            session = SteelBrowserProvider().create_session("task-degrade-2")
        assert session["degraded_features"] == ["captcha_solving"]

    def test_features_report_actual_captcha_state_from_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report what Steel granted (solveCaptcha), not what we asked for —
        the account may silently run without a requested feature."""
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "true")
        granted = {
            "id": "s1",
            "websocketUrl": "wss://connect.steel.dev?sessionId=s1",
            "solveCaptcha": True,
        }
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(200, granted)
        ):
            session = SteelBrowserProvider().create_session("task-granted")
        assert session["features"]["proxy"] is False
        assert session["features"]["captcha_solving"] is True
        assert "degraded_features" not in session

    def test_silently_dropped_captcha_is_reported_as_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Steel returns 200 but reports solveCaptcha false instead of 402 —
        the session runs without it, so say so honestly."""
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "true")
        silently_dropped = {
            "id": "s1",
            "websocketUrl": "wss://connect.steel.dev?sessionId=s1",
            "solveCaptcha": False,
        }
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, silently_dropped),
        ):
            session = SteelBrowserProvider().create_session("task-dropped")
        assert session["features"]["captcha_solving"] is False
        assert session["degraded_features"] == ["captcha_solving"]

    def test_absent_captcha_state_falls_back_to_requested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-hosted / older Steel may omit solveCaptcha entirely — don't
        cry degradation when we simply can't verify."""
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "true")
        payload = {
            "id": "s1",
            "websocketUrl": "wss://connect.steel.dev?sessionId=s1",
        }
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(200, payload)
        ):
            session = SteelBrowserProvider().create_session("task-absent")
        assert session["features"]["captcha_solving"] is True
        assert "degraded_features" not in session

    def test_402_without_features_raises_with_pricing_pointer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(402, None)
        ):
            with pytest.raises(RuntimeError, match="pricinglimits"):
                SteelBrowserProvider().create_session("task-402-bare")

    def test_402_retry_still_failing_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "true")
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[
                FakeResponse(402, {"message": "no"}),
                FakeResponse(500, None),
            ],
        ):
            with pytest.raises(RuntimeError, match="Failed to create Steel session"):
                SteelBrowserProvider().create_session("task-degrade-3")

    def test_request_body_reflects_env_knobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        monkeypatch.setenv("STEEL_PROXY_COUNTRY", "DE")
        monkeypatch.setenv("DATAIMPULSE_PROXY_HOST", "gw.dataimpulse.com")
        monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "823")
        monkeypatch.setenv("DATAIMPULSE_PROXY_USERNAME", "login")
        monkeypatch.setenv("DATAIMPULSE_PROXY_PASSWORD", "pass")
        monkeypatch.setenv("STEEL_SOLVE_CAPTCHA", "1")
        monkeypatch.setenv("STEEL_SESSION_TIMEOUT", "600000")

        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            session = SteelBrowserProvider().create_session("task-2")

        body = post.call_args.kwargs["json"]
        assert "useProxy" not in body
        assert body["proxyUrl"].startswith("http://")
        assert "login__cr.de" in body["proxyUrl"]
        assert body["solveCaptcha"] is True
        assert body["timeout"] == 600000
        assert session["features"] == {
            "proxy": True,
            "captcha_solving": True,
            "custom_timeout": True,
        }

    def test_default_timeout_is_agent_sized_not_steels_5_minutes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Steel's API default (300000 ms = 5 min) kills long agent tasks
        mid-flight; without an override the plugin must request a longer
        hard cap plus a server-side inactivity timeout as crash insurance."""
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            session = SteelBrowserProvider().create_session("task-default-timeout")
        body = post.call_args.kwargs["json"]
        assert body["timeout"] == 1_800_000
        assert body["inactivityTimeout"] == 600_000
        # A plugin-chosen default is not a user-chosen custom timeout.
        assert session["features"]["custom_timeout"] is False

    def test_invalid_timeout_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_SESSION_TIMEOUT", "not-a-number")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            session = SteelBrowserProvider().create_session("task-3")
        assert post.call_args.kwargs["json"]["timeout"] == 1_800_000
        assert session["features"]["custom_timeout"] is False

    def test_missing_api_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="STEEL_API_KEY"):
            SteelBrowserProvider().create_session("task-4")

    def test_non_ok_response_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(402, None)
        ):
            with pytest.raises(RuntimeError, match="Failed to create Steel session"):
                SteelBrowserProvider().create_session("task-5")

    def test_response_missing_fields_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[FakeResponse(200, {"id": "x"}), FakeResponse(204, {})],
        ) as post:
            with pytest.raises(RuntimeError, match="missing websocketUrl"):
                SteelBrowserProvider().create_session("task-6")
        assert post.call_count == 2
        assert post.call_args_list[1].args[0].endswith("/v1/sessions/x/release")

    def test_response_missing_id_does_not_attempt_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, {"websocketUrl": "wss://connect.steel.dev"}),
        ) as post:
            with pytest.raises(RuntimeError, match="missing or invalid id"):
                SteelBrowserProvider().create_session("task-7")
        assert post.call_count == 1

    def test_missing_top_level_id_recovers_websocket_id_for_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {
            "websocketUrl": "wss://connect.steel.dev?sessionId=sess-recovered"
        }
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[FakeResponse(200, payload), FakeResponse(204, {})],
        ) as post:
            with pytest.raises(RuntimeError, match="missing or invalid id"):
                SteelBrowserProvider().create_session("task-missing-id")
        assert post.call_count == 2
        assert post.call_args_list[1].args[0].endswith(
            "/v1/sessions/sess-recovered/release"
        )

    @pytest.mark.parametrize("invalid_id", [None, "", "   ", 123, ["sess-recovered"]])
    def test_invalid_top_level_id_recovers_websocket_id_for_release(
        self, monkeypatch: pytest.MonkeyPatch, invalid_id: object
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {
            "id": invalid_id,
            "websocketUrl": (
                "wss://connect.steel.dev?sessionId=sess-recovered"
            ),
        }
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[FakeResponse(200, payload), FakeResponse(204, {})],
        ) as post:
            with pytest.raises(RuntimeError, match="missing or invalid id"):
                SteelBrowserProvider().create_session("task-invalid-id")
        assert post.call_count == 2
        assert post.call_args_list[1].args[0].endswith(
            "/v1/sessions/sess-recovered/release"
        )

    def test_ambiguous_websocket_ids_are_not_used_to_release_missing_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {
            "websocketUrl": (
                "wss://connect.steel.dev?sessionId=one&sessionId=two"
            ),
        }
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, payload),
        ) as post:
            with pytest.raises(RuntimeError, match="missing or invalid id"):
                SteelBrowserProvider().create_session("task-ambiguous-id")
        assert post.call_count == 1

    def test_normalizes_whitespace_around_top_level_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {
            "id": "  sess-normalized  ",
            "websocketUrl": (
                "wss://connect.steel.dev?sessionId=sess-normalized"
            ),
        }
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, payload),
        ):
            session = SteelBrowserProvider().create_session("task-normalized-id")
        assert session["bb_session_id"] == "sess-normalized"

    @pytest.mark.parametrize(
        "websocket_url",
        [
            "wss://connect.steel.dev?sessionId=other-session",
            (
                "wss://connect.steel.dev?sessionId=sess-created"
                "&sessionId=other-session"
            ),
            "wss://connect.steel.dev?sessionId=",
        ],
    )
    def test_mismatched_websocket_id_releases_top_level_session_and_raises(
        self, monkeypatch: pytest.MonkeyPatch, websocket_url: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {"id": "sess-created", "websocketUrl": websocket_url}
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[FakeResponse(200, payload), FakeResponse(204, {})],
        ) as post:
            with pytest.raises(RuntimeError, match="did not match"):
                SteelBrowserProvider().create_session("task-mismatched-id")
        assert post.call_count == 2
        assert post.call_args_list[1].args[0].endswith(
            "/v1/sessions/sess-created/release"
        )

    def test_invalid_websocket_url_releases_created_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {"id": "sess-invalid", "websocketUrl": "not-a-websocket-url"}
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[FakeResponse(200, payload), FakeResponse(204, {})],
        ) as post:
            with pytest.raises(RuntimeError, match="invalid websocketUrl"):
                SteelBrowserProvider().create_session("task-8")
        assert post.call_count == 2
        assert post.call_args_list[1].args[0].endswith(
            "/v1/sessions/sess-invalid/release"
        )


# ---------------------------------------------------------------------------
# hermes-native config knobs (browser.steel.* in config.yaml)
# ---------------------------------------------------------------------------


class TestHermesConfigKnobs:
    def _write_config(self, tmp_path, monkeypatch, body: str) -> None:
        home = tmp_path / "hermes-home"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(body)
        monkeypatch.setenv("HERMES_HOME", str(home))

    def test_config_yaml_knobs_apply_without_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("DATAIMPULSE_PROXY_HOST", "gw.dataimpulse.com")
        monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "823")
        monkeypatch.setenv("DATAIMPULSE_PROXY_USERNAME", "login")
        monkeypatch.setenv("DATAIMPULSE_PROXY_PASSWORD", "pass")
        self._write_config(
            tmp_path,
            monkeypatch,
            "browser:\n  steel:\n    use_proxy: true\n"
            "    proxy_country: DE\n"
            "    solve_captcha: true\n    session_timeout: 900000\n",
        )
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            session = SteelBrowserProvider().create_session("task-cfg")
        body = post.call_args.kwargs["json"]
        assert "useProxy" not in body
        assert "login__cr.de" in body["proxyUrl"]
        assert body["solveCaptcha"] is True
        assert body["timeout"] == 900000
        assert session["features"] == {
            "proxy": True,
            "captcha_solving": True,
            "custom_timeout": True,
        }

    def test_env_var_overrides_config_yaml(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_USE_PROXY", "false")
        self._write_config(
            tmp_path, monkeypatch, "browser:\n  steel:\n    use_proxy: true\n"
        )
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            SteelBrowserProvider().create_session("task-cfg-override")
        assert "useProxy" not in post.call_args.kwargs["json"]

    def test_config_base_url_enables_self_hosted_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        self._write_config(
            tmp_path,
            monkeypatch,
            "browser:\n  steel:\n    base_url: http://steel.internal.example\n",
        )
        provider = SteelBrowserProvider()
        assert provider.is_available() is True
        payload = {
            "id": "cfg-1",
            "websocketUrl": "ws://steel.internal.example?sessionId=cfg-1",
        }
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, payload),
        ) as post:
            provider.create_session("task-cfg-base")
        assert post.call_args.args[0] == "http://steel.internal.example/v1/sessions"


# ---------------------------------------------------------------------------
# close_session / emergency_cleanup
# ---------------------------------------------------------------------------


class TestCloseSession:
    def test_release_endpoint_called_and_true_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(204, {})
        ) as post:
            ok = SteelBrowserProvider().close_session("sess-123")
        assert ok is True
        assert post.call_args.args[0].endswith("/v1/sessions/sess-123/release")

    def test_false_when_api_key_missing(self) -> None:
        assert SteelBrowserProvider().close_session("sess-123") is False

    def test_false_on_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with (
            patch.object(
                steel_module.requests, "post", return_value=FakeResponse(500, None)
            ) as post,
            patch.object(steel_module.time, "sleep"),
        ):
            assert SteelBrowserProvider().close_session("sess-123") is False
        assert post.call_count == 3

    def test_retries_429_and_server_errors_until_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with (
            patch.object(
                steel_module.requests,
                "post",
                side_effect=[
                    FakeResponse(503, None),
                    FakeResponse(429, None),
                    FakeResponse(204, {}),
                ],
            ) as post,
            patch.object(steel_module.time, "sleep") as sleep,
        ):
            assert SteelBrowserProvider().close_session("sess-123") is True
        assert post.call_count == 3
        assert sleep.call_count == 2

    def test_retries_http_request_timeout_until_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with (
            patch.object(
                steel_module.requests,
                "post",
                side_effect=[FakeResponse(408, None), FakeResponse(204, {})],
            ) as post,
            patch.object(steel_module.time, "sleep") as sleep,
        ):
            assert SteelBrowserProvider().close_session("sess-123") is True
        assert post.call_count == 2
        sleep.assert_called_once()

    def test_retries_request_exception_until_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with (
            patch.object(
                steel_module.requests,
                "post",
                side_effect=[
                    steel_module.requests.ConnectionError("network down"),
                    FakeResponse(204, {}),
                ],
            ) as post,
            patch.object(steel_module.time, "sleep") as sleep,
        ):
            assert SteelBrowserProvider().close_session("sess-123") is True
        assert post.call_count == 2
        sleep.assert_called_once()

    def test_does_not_retry_ordinary_client_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(400, None)
        ) as post:
            assert SteelBrowserProvider().close_session("sess-123") is False
        assert post.call_count == 1

    @pytest.mark.parametrize("status_code", [404, 410])
    def test_already_released_status_is_success(
        self, monkeypatch: pytest.MonkeyPatch, status_code: int
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(status_code, None),
        ) as post:
            assert SteelBrowserProvider().close_session("sess-123") is True
        assert post.call_count == 1

    def test_self_hosted_release_omits_auth_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal.example")
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(204, {})
        ) as post:
            assert SteelBrowserProvider().close_session("sess-123") is True
        assert post.call_args.kwargs["headers"] == {
            "Content-Type": "application/json"
        }

    def test_release_url_encodes_session_id_as_single_path_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests, "post", return_value=FakeResponse(204, {})
        ) as post:
            assert SteelBrowserProvider().close_session("folder/id ?#%") is True
        assert post.call_args.args[0].endswith(
            "/v1/sessions/folder%2Fid%20%3F%23%25/release"
        )

    def test_emergency_cleanup_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests, "post", side_effect=OSError("network down")
        ):
            SteelBrowserProvider().emergency_cleanup("sess-123")


# ---------------------------------------------------------------------------
# Picker schema
# ---------------------------------------------------------------------------


class TestSetupSchema:
    def test_schema_shape(self) -> None:
        schema = SteelBrowserProvider().get_setup_schema()
        assert schema["name"] == "Steel"
        assert schema["post_setup"] == "agent_browser"
        keys = [e["key"] for e in schema["env_vars"]]
        assert keys == ["STEEL_API_KEY"]

    def test_self_hosted_schema_does_not_require_cloud_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal.example")
        schema = SteelBrowserProvider().get_setup_schema()
        assert schema["env_vars"] == []


# ---------------------------------------------------------------------------
# Host registry: explicit-only selection
# ---------------------------------------------------------------------------


class TestRegistrySelection:
    def test_explicit_steel_resolves_even_when_unavailable(self) -> None:
        from agent.browser_registry import _resolve, register_provider

        register_provider(SteelBrowserProvider())
        provider = _resolve("steel")
        assert provider is not None
        assert provider.name == "steel"
        assert provider.is_available() is False

    def test_steel_not_auto_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Steel is explicit-only — having STEEL_API_KEY set must not
        auto-route a user to the paid cloud browser."""
        from agent.browser_registry import _resolve, register_provider

        register_provider(SteelBrowserProvider())
        monkeypatch.setenv("STEEL_API_KEY", "k")
        assert _resolve(None) is None


# ---------------------------------------------------------------------------
# Steel Credentials API (browser.steel.credentials)
# ---------------------------------------------------------------------------


class TestCredentialsConfig:
    def _write_config(self, tmp_path, monkeypatch, body: str) -> None:
        home = tmp_path / "hermes-home-credentials"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(body)
        monkeypatch.setenv("HERMES_HOME", str(home))

    def test_enabled_config_passes_namespace_and_origins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        self._write_config(
            tmp_path,
            monkeypatch,
            "browser:\n  steel:\n    credentials:\n      enabled: true\n"
            "      namespace: production-agent-a\n"
            "      origins:\n        - https://example.com\n"
            "        - https://admin.example.com\n",
        )
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            SteelBrowserProvider().create_session("task-creds")

        body = post.call_args.kwargs["json"]
        assert body["namespace"] == "production-agent-a"
        assert body["credentials"] == {
            "origins": ["https://example.com", "https://admin.example.com"]
        }

    def test_origins_are_normalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        self._write_config(
            tmp_path,
            monkeypatch,
            "browser:\n  steel:\n    credentials:\n      enabled: true\n"
            "      origins:\n        - HTTPS://Example.COM/\n",
        )
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            SteelBrowserProvider().create_session("task-creds-normalized")
        body = post.call_args.kwargs["json"]
        assert body["credentials"]["origins"] == ["https://example.com"]
        assert body["namespace"] == "default"

    @pytest.mark.parametrize(
        "config_body",
        [
            "browser:\n  steel:\n    credentials:\n      enabled: false\n"
            "      origins:\n        - https://example.com\n",
            "browser:\n  steel:\n    credentials:\n"
            "      origins:\n        - https://example.com\n",
            "browser:\n  steel:\n    use_proxy: false\n",
        ],
    )
    def test_disabled_or_absent_credentials_add_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        config_body: str,
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        self._write_config(tmp_path, monkeypatch, config_body)
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            SteelBrowserProvider().create_session("task-creds-off")
        body = post.call_args.kwargs["json"]
        assert "namespace" not in body
        assert "credentials" not in body

    @pytest.mark.parametrize(
        "credentials_yaml",
        [
            "      enabled: true\n",
            "      enabled: true\n      origins: []\n",
            "      enabled: true\n      origins: https://example.com\n",
            "      enabled: true\n      origins:\n        - example.com\n",
            "      enabled: true\n      origins:\n"
            "        - https://user:pass@example.com\n",
            "      enabled: true\n      origins:\n"
            "        - https://example.com/path\n",
            "      enabled: true\n      origins:\n"
            "        - https://example.com?q=1\n",
            "      enabled: true\n      origins:\n"
            "        - https://example.com\n      namespace: ..\n",
        ],
    )
    def test_invalid_credentials_config_fails_before_session_creation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, credentials_yaml: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        self._write_config(
            tmp_path,
            monkeypatch,
            "browser:\n  steel:\n    credentials:\n" + credentials_yaml,
        )
        with patch.object(steel_module.requests, "post") as post:
            with pytest.raises(ValueError, match="credentials"):
                SteelBrowserProvider().create_session("task-creds-invalid")
        post.assert_not_called()


# ---------------------------------------------------------------------------
# Dataimpulse proxy session payload
# ---------------------------------------------------------------------------


class TestDataimpulseSessionPayload:
    def _proxy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        monkeypatch.setenv("STEEL_PROXY_COUNTRY", "DE")
        monkeypatch.setenv("DATAIMPULSE_PROXY_HOST", "gw.dataimpulse.com")
        monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "823")
        monkeypatch.setenv("DATAIMPULSE_PROXY_USERNAME", "login")
        monkeypatch.setenv("DATAIMPULSE_PROXY_PASSWORD", "p@ss:w/rd?")

    def test_proxy_disabled_by_default_has_no_proxy_parameters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(200, _ok_session_payload()),
        ) as post:
            SteelBrowserProvider().create_session("task-no-proxy")
        body = post.call_args.kwargs["json"]
        assert "proxyUrl" not in body
        assert "useProxy" not in body

    def test_disabling_after_enabling_removes_proxy_from_next_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes_steel import config

        self._proxy_env(monkeypatch)
        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[
                FakeResponse(200, _ok_session_payload()),
                FakeResponse(200, _ok_session_payload()),
            ],
        ) as post:
            provider = SteelBrowserProvider()
            provider.create_session("task-toggle")
            assert "proxyUrl" in post.call_args_list[0].kwargs["json"]

            config.set_session_override("use_proxy", False, "task-toggle")
            provider.create_session("task-toggle")
            assert "proxyUrl" not in post.call_args_list[1].kwargs["json"]

    def test_missing_country_fails_before_creating_a_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._proxy_env(monkeypatch)
        monkeypatch.delenv("STEEL_PROXY_COUNTRY")
        with patch.object(steel_module.requests, "post") as post:
            with pytest.raises(ValueError, match="proxy_country"):
                SteelBrowserProvider().create_session("task-no-country")
        post.assert_not_called()

    def test_missing_proxy_credentials_fail_before_creating_a_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._proxy_env(monkeypatch)
        monkeypatch.delenv("DATAIMPULSE_PROXY_PASSWORD")
        with patch.object(steel_module.requests, "post") as post:
            with pytest.raises(ValueError, match="DATAIMPULSE_PROXY_PASSWORD"):
                SteelBrowserProvider().create_session("task-no-creds")
        post.assert_not_called()

    def test_proxy_rejection_never_falls_back_to_direct_browsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._proxy_env(monkeypatch)
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(402, {"message": "proxy rejected"}),
        ) as post:
            with pytest.raises(RuntimeError, match="proxy was not removed"):
                SteelBrowserProvider().create_session("task-proxy-402")
        assert post.call_count == 1
        assert "proxyUrl" in post.call_args.kwargs["json"]
        assert "p@ss:w/rd?" not in str(post.call_args.kwargs["json"]["proxyUrl"])
