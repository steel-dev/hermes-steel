# ABOUTME: Tests for the steel_session_options tool — per-task proxy / country /
# ABOUTME: CAPTCHA tuning and teardown of the old session to apply them.
"""Tests for agent-driven session tuning.

The agent watches results, so it decides per site whether the Dataimpulse
proxy, its country, or CAPTCHA solving help or hurt. steel_session_options
stores those as per-task overrides and tears down the live session so the next
browser_* call builds a fresh one with the chosen features.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hermes_steel import config
from hermes_steel.session_options import steel_session_options


@pytest.fixture(autouse=True)
def _clear_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "_session_overrides", {})


@pytest.fixture
def _proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_PROXY_HOST", "gw.dataimpulse.com")
    monkeypatch.setenv("DATAIMPULSE_PROXY_PORT", "823")
    monkeypatch.setenv("DATAIMPULSE_PROXY_USERNAME", "login")
    monkeypatch.setenv("DATAIMPULSE_PROXY_PASSWORD", "pass")


class TestOverridePrecedence:
    def test_override_beats_env_for_its_own_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "false")
        config.set_session_override("use_proxy", True, "task-1")
        assert (
            config.steel_setting("STEEL_USE_PROXY", "use_proxy", "task-1")
            == "true"
        )

    def test_override_does_not_leak_to_another_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "false")
        config.set_session_override("use_proxy", True, "task-1")
        assert (
            config.steel_setting("STEEL_USE_PROXY", "use_proxy", "task-2")
            == "false"
        )

    def test_override_can_force_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        config.set_session_override("use_proxy", False, "task-1")
        assert (
            config.steel_setting("STEEL_USE_PROXY", "use_proxy", "task-1")
            == "false"
        )

    def test_clearing_override_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        config.set_session_override("use_proxy", True, "task-1")
        config.set_session_override("use_proxy", None, "task-1")
        assert (
            config.steel_setting("STEEL_USE_PROXY", "use_proxy", "task-1")
            == "true"
        )


class TestSteelSessionOptions:
    def test_enables_proxy_country_and_captcha_then_tears_down(self) -> None:
        from tools import browser_tool

        with patch.object(browser_tool, "cleanup_browser") as cleanup:
            out = json.loads(
                steel_session_options(
                    use_proxy=True,
                    proxy_country="de",
                    solve_captcha=True,
                    task_id="task-1",
                )
            )
        assert out["applied"] == {
            "use_proxy": True,
            "proxy_country": "DE",
            "solve_captcha": True,
        }
        assert config._session_overrides == {
            "task-1": {
                "use_proxy": "true",
                "proxy_country": "DE",
                "solve_captcha": "true",
            }
        }
        cleanup.assert_called_once_with("task-1")
        assert "browser_navigate" in out["message"]

    def test_can_disable_proxy_and_clears_country(self) -> None:
        from tools import browser_tool

        config.set_session_override("proxy_country", "DE", "task-1")
        with patch.object(browser_tool, "cleanup_browser"):
            steel_session_options(use_proxy=False, task_id="task-1")
        assert config._session_overrides == {"task-1": {"use_proxy": "false"}}

    def test_omitted_feature_is_left_unchanged(self) -> None:
        from tools import browser_tool

        config.set_session_override("solve_captcha", True, "task-1")
        with patch.object(browser_tool, "cleanup_browser"):
            steel_session_options(
                use_proxy=True, proxy_country="US", task_id="task-1"
            )
        assert config._session_overrides == {
            "task-1": {
                "solve_captcha": "true",
                "use_proxy": "true",
                "proxy_country": "US",
            }
        }

    def test_missing_country_is_a_validation_error_without_teardown(self) -> None:
        from tools import browser_tool

        with patch.object(browser_tool, "cleanup_browser") as cleanup:
            out = json.loads(
                steel_session_options(use_proxy=True, task_id="task-1")
            )
        assert "proxy_country is required" in out["error"]
        assert config._session_overrides == {}
        cleanup.assert_not_called()

    @pytest.mark.parametrize("country", ["Germany", "ZZ", "D", "1A", ""])
    def test_invalid_country_is_rejected(self, country: str) -> None:
        with patch("tools.browser_tool.cleanup_browser") as cleanup:
            out = json.loads(
                steel_session_options(
                    use_proxy=True, proxy_country=country, task_id="task-1"
                )
            )
        assert "proxy_country" in out["error"]
        assert config._session_overrides == {}
        cleanup.assert_not_called()

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
            out = json.loads(
                steel_session_options(
                    use_proxy=True, proxy_country="DE", task_id="task-1"
                )
            )
        assert out["applied"]["use_proxy"] is True


class TestProviderReadsOverride:
    def test_create_session_honors_agent_override_over_disabled_env(
        self, monkeypatch: pytest.MonkeyPatch, _proxy_env
    ) -> None:
        from conftest import FakeResponse
        from hermes_steel import provider as steel_module
        from hermes_steel.provider import SteelBrowserProvider

        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_USE_PROXY", "false")
        config.set_session_override("use_proxy", True, "task-x")
        config.set_session_override("proxy_country", "DE", "task-x")
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
        body = post.call_args.kwargs["json"]
        assert "useProxy" not in body
        assert "login__cr.de" in body["proxyUrl"]

    def test_concurrent_tasks_do_not_inherit_proxy_settings(
        self, monkeypatch: pytest.MonkeyPatch, _proxy_env
    ) -> None:
        from conftest import FakeResponse
        from hermes_steel import provider as steel_module
        from hermes_steel.provider import SteelBrowserProvider

        monkeypatch.setenv("STEEL_API_KEY", "secret")
        config.set_session_override("use_proxy", True, "agent-a")
        config.set_session_override("proxy_country", "DE", "agent-a")
        config.set_session_override("use_proxy", True, "agent-b")
        config.set_session_override("proxy_country", "US", "agent-b")

        with patch.object(
            steel_module.requests,
            "post",
            side_effect=[
                FakeResponse(200, _ok_session("s-a")),
                FakeResponse(200, _ok_session("s-b")),
                FakeResponse(200, _ok_session("s-c")),
            ],
        ) as post:
            provider = SteelBrowserProvider()
            provider.create_session("agent-a")
            provider.create_session("agent-b")
            provider.create_session("agent-c")

        body_a = post.call_args_list[0].kwargs["json"]
        body_b = post.call_args_list[1].kwargs["json"]
        body_c = post.call_args_list[2].kwargs["json"]
        assert "login__cr.de" in body_a["proxyUrl"]
        assert "login__cr.us" in body_b["proxyUrl"]
        assert "proxyUrl" not in body_c


def _ok_session(session_id: str) -> dict:
    return {
        "id": session_id,
        "websocketUrl": f"wss://connect.steel.dev?sessionId={session_id}",
    }
