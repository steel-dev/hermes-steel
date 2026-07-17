# ABOUTME: Tests for the steel_session_options tool — the agent tuning proxy /
# ABOUTME: CAPTCHA per session and tearing down the old session to apply them.
"""Tests for agent-driven session tuning.

The agent watches results, so it decides per site whether proxy / CAPTCHA
solving help or hurt. steel_session_options sets those as highest-precedence
overrides and tears down the live session so the next browser_* call builds a
fresh one with the chosen features.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hermes_steel import config, session_options
from hermes_steel.session_options import steel_session_options


@pytest.fixture(autouse=True)
def _clear_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "_session_overrides", {})


class TestOverridePrecedence:
    def test_override_beats_env_and_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "false")
        config.set_session_override("use_proxy", True)
        assert config.steel_setting("STEEL_USE_PROXY", "use_proxy") == "true"

    def test_override_can_force_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        config.set_session_override("use_proxy", False)
        assert config.steel_setting("STEEL_USE_PROXY", "use_proxy") == "false"

    def test_clearing_override_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        config.set_session_override("use_proxy", True)
        config.set_session_override("use_proxy", None)
        assert config.steel_setting("STEEL_USE_PROXY", "use_proxy") == "true"


class TestSteelSessionOptions:
    def test_enables_features_and_tears_down_session(self) -> None:
        from tools import browser_tool

        with patch.object(browser_tool, "cleanup_browser") as cleanup:
            out = json.loads(
                steel_session_options(
                    use_proxy=True, solve_captcha=True, task_id="task-1"
                )
            )
        assert out["applied"] == {"use_proxy": True, "solve_captcha": True}
        assert config._session_overrides == {
            "use_proxy": "true",
            "solve_captcha": "true",
        }
        cleanup.assert_called_once_with("task-1")
        assert "browser_navigate" in out["message"]

    def test_can_disable_a_single_feature(self) -> None:
        from tools import browser_tool

        with patch.object(browser_tool, "cleanup_browser"):
            steel_session_options(use_proxy=False, task_id=None)
        assert config._session_overrides == {"use_proxy": "false"}

    def test_omitted_feature_is_left_unchanged(self) -> None:
        from tools import browser_tool

        config.set_session_override("solve_captcha", True)
        with patch.object(browser_tool, "cleanup_browser"):
            steel_session_options(use_proxy=True, task_id=None)
        assert config._session_overrides == {
            "solve_captcha": "true",
            "use_proxy": "true",
        }

    def test_no_options_is_a_noop_error_without_teardown(self) -> None:
        from tools import browser_tool

        with patch.object(browser_tool, "cleanup_browser") as cleanup:
            out = json.loads(steel_session_options(task_id="task-1"))
        assert "error" in out
        cleanup.assert_not_called()

    def test_teardown_failure_still_reports_applied(self) -> None:
        from tools import browser_tool

        with patch.object(
            browser_tool, "cleanup_browser", side_effect=RuntimeError("boom")
        ):
            out = json.loads(steel_session_options(use_proxy=True, task_id=None))
        assert out["applied"] == {"use_proxy": True}


class TestProviderReadsOverride:
    def test_create_session_honors_agent_override_over_disabled_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conftest import FakeResponse
        from hermes_steel import provider as steel_module
        from hermes_steel.provider import SteelBrowserProvider

        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_USE_PROXY", "false")
        config.set_session_override("use_proxy", True)
        with patch.object(
            steel_module.requests,
            "post",
            return_value=FakeResponse(
                200,
                {
                    "id": "s1",
                    "websocketUrl": "wss://connect.steel.dev?sessionId=s1",
                },
            ),
        ) as post:
            SteelBrowserProvider().create_session("task-x")
        assert post.call_args.kwargs["json"]["useProxy"] is True
