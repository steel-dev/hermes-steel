# ABOUTME: Unit tests for handoff detection — the note that tells the agent a
# ABOUTME: page needs a human and points it at the Steel live viewer.
"""Tests for the Steel handoff hint.

Handoff detection rides on the session-note hook (the host applies only the
first string a ``transform_tool_result`` callback returns), so these tests
drive it through ``steel_session_note`` exactly as the host would, plus a few
unit-level checks of the detector itself.

The bar is asymmetric on purpose: missing a login wall costs the agent one
wasted retry, while a false alarm interrupts the user for nothing. So the
"stays quiet" cases matter at least as much as the "fires" ones.
"""

from __future__ import annotations

import json

import pytest

from hermes_steel import handoff, session_note
from hermes_steel.provider import SteelBrowserProvider
from hermes_steel.session_note import steel_session_note


def _steel_session(session_id: str = "sess-1") -> dict:
    return {
        "session_name": "hermes_abc_12345678",
        "bb_session_id": session_id,
        "cdp_url": f"wss://connect.steel.dev/?sessionId={session_id}&apiKey=k",
        "features": {},
        "session_viewer_url": f"https://app.steel.dev/sessions/{session_id}",
    }


def _result(**fields) -> str:
    payload = {"success": True}
    payload.update(fields)
    return json.dumps(payload)


def _login_page() -> str:
    return _result(
        url="https://example.com/account/login",
        title="Sign in — Example",
        snapshot=(
            '- heading "Sign in to Example" [ref=e1]\n'
            '- textbox "Email" [ref=e2]\n'
            '- textbox "Password" [ref=e3]\n'
            '- button "Sign in" [ref=e4]\n'
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate note/handoff tracking and the browser_tool surface."""
    from tools import browser_tool

    monkeypatch.setattr(session_note, "_noted_session_ids", set())
    monkeypatch.setattr(session_note, "_hinted_session_ids", set())
    monkeypatch.setattr(handoff, "_handoff_session_ids", set())
    monkeypatch.setattr(browser_tool, "_active_sessions", {}, raising=True)
    monkeypatch.setattr(
        browser_tool, "_get_cloud_provider", lambda: SteelBrowserProvider()
    )
    browser_tool._active_sessions["default"] = _steel_session()
    return browser_tool


def _note(result: str, tool_name: str = "browser_navigate", **kwargs):
    return steel_session_note(
        tool_name=tool_name, args={}, result=result, task_id=None, **kwargs
    )


def _consume_backend_note() -> None:
    """Take the once-per-session "running on Steel" note out of the way.

    It rides the same hook, so without this every first call returns a
    string and the "stays quiet" assertions would pass for the wrong reason.
    """
    assert _note(_result(url="https://example.com/", title="Example")) is not None


class TestHandoffFires:
    def test_login_wall_asks_for_a_human_at_the_viewer(self) -> None:
        out = _note(_login_page())
        assert out is not None
        assert "Human input is likely needed" in out
        assert "sign-in wall" in out
        assert "https://app.steel.dev/sessions/sess-1" in out

    def test_note_tells_the_agent_to_stop_retrying(self) -> None:
        out = _note(_login_page())
        assert out is not None
        assert "Stop retrying it automatically" in out
        assert "Do not guess credentials" in out

    @pytest.mark.parametrize(
        "fields, reason",
        [
            (
                {"title": "Verify you are human", "url": "https://shop.example/checkout"},
                "human-verification",
            ),
            (
                {
                    "url": "https://example.com/home",
                    "title": "Example",
                    "snapshot": '- heading "Complete the CAPTCHA to continue" [ref=e1]',
                },
                "human-verification",
            ),
            (
                {
                    "url": "https://example.com/home",
                    "title": "Example",
                    "snapshot": (
                        '- heading "Two-factor authentication" [ref=e1]\n'
                        '- textbox "Verification code" [ref=e2]\n'
                    ),
                },
                "two-factor",
            ),
            (
                {
                    "url": "https://example.com/home",
                    "title": "Example",
                    "snapshot": '- heading "Enter the code we sent to your phone" [ref=e1]',
                },
                "two-factor",
            ),
            (
                {"title": "3-D Secure", "url": "https://bank.example/pay"},
                "payment confirmation",
            ),
            (
                {"url": "https://example.com/challenge/2fa", "title": "Example"},
                "two-factor",
            ),
            (
                {"url": "https://example.com/session/new", "title": "Example"},
                "sign-in wall",
            ),
        ],
    )
    def test_recognizes_common_handoff_pages(self, fields: dict, reason: str) -> None:
        out = _note(_result(**fields))
        assert out is not None
        assert "Human input is likely needed" in out
        assert reason in out

    def test_password_field_alone_is_enough(self) -> None:
        """No auth URL, no auth title — a rendered password box is asking."""
        out = _note(
            _result(
                url="https://example.com/",
                title="Example",
                snapshot=(
                    '- heading "Log in" [ref=e1]\n'
                    '- textbox "Password" [ref=e2]\n'
                ),
            )
        )
        assert out is not None
        assert "sign-in wall" in out

    def test_tool_reported_block_is_taken_at_face_value(self) -> None:
        out = _note(
            _result(
                url="https://example.com/",
                title="Access Denied",
                bot_detection_warning="Page title 'Access Denied' suggests bot detection.",
            )
        )
        assert out is not None
        assert "blocked or bot-challenged" in out

    def test_note_without_viewer_url_still_asks_for_a_human(self) -> None:
        session = _steel_session()
        del session["session_viewer_url"]
        from tools import browser_tool

        browser_tool._active_sessions["default"] = session
        out = _note(_login_page())
        assert out is not None
        assert "Human input is likely needed" in out
        assert "live viewer" not in out

    def test_detection_is_logged_for_diagnosis(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="hermes_steel.handoff"):
            _note(_login_page())
        assert any("Steel handoff detected" in r.message for r in caplog.records)


class TestHandoffStaysQuiet:
    def test_ordinary_page_does_not_fire(self) -> None:
        _consume_backend_note()
        out = _note(
            _result(
                url="https://example.com/",
                title="Example Domain",
                snapshot=(
                    '- heading "Example Domain" [ref=e1]\n'
                    '- link "More information..." [ref=e2]\n'
                ),
            ),
            tool_name="browser_snapshot",
        )
        assert out is None

    def test_article_merely_mentioning_login_does_not_fire(self) -> None:
        """The false positive that matters: prose about logins, and the
        sign-in link every content site puts in its header."""
        _consume_backend_note()
        out = _note(
            _result(
                url="https://news.example.com/2026/passwords-are-dead",
                title="Passwords are dead: why login flows are changing",
                snapshot=(
                    '- heading "Passwords are dead" [ref=e1]\n'
                    '- link "Sign in" [ref=e2]\n'
                    '- text "Readers still log in with a password every day, '
                    'and captcha fatigue is real."\n'
                ),
            ),
            tool_name="browser_snapshot",
        )
        assert out is None

    def test_auth_flavoured_hostname_alone_does_not_fire(self) -> None:
        _consume_backend_note()
        out = _note(
            _result(
                url="https://login.example.com/docs/getting-started",
                title="Getting started — Example Docs",
                snapshot='- heading "Getting started" [ref=e1]',
            ),
            tool_name="browser_snapshot",
        )
        assert out is None

    def test_fires_only_once_per_session(self) -> None:
        first = _note(_login_page())
        assert first is not None
        second = _note(_login_page(), tool_name="browser_snapshot")
        assert second is None

    def test_new_session_gets_its_own_handoff_note(self, _fresh_state) -> None:
        assert _note(_login_page()) is not None
        _fresh_state._active_sessions["default"] = _steel_session("sess-2")
        out = _note(_login_page())
        assert out is not None
        assert "Human input is likely needed" in out
        assert "https://app.steel.dev/sessions/sess-2" in out

    def test_disabled_by_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEEL_HANDOFF_DETECTION", "false")
        _consume_backend_note()
        out = _note(_login_page(), tool_name="browser_snapshot")
        assert out is None

    def test_disabled_via_hermes_config(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "browser:\n  steel:\n    handoff_detection: false\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        assert handoff.handoff_enabled() is False
        _consume_backend_note()
        assert _note(_login_page(), tool_name="browser_snapshot") is None

    def test_enabled_by_default(self) -> None:
        assert handoff.handoff_enabled() is True

    def test_error_results_are_deferred_like_the_backend_note(self) -> None:
        """A bare navigation timeout must not burn the one-shot note."""
        assert _note(_login_page(), status="error") is None
        _consume_backend_note()
        out = _note(_login_page(), tool_name="browser_snapshot")
        assert out is not None
        assert "Human input is likely needed" in out

    def test_non_browser_tools_are_ignored(self) -> None:
        _consume_backend_note()
        assert _note(_login_page(), tool_name="steel_scrape") is None


class TestHandoffDetector:
    """Unit-level checks of the detector, independent of the hook."""

    def test_non_json_result_matches_only_strong_markers(self) -> None:
        assert handoff._handoff_reason("Please complete the CAPTCHA") is not None
        assert handoff._handoff_reason("Sign in is available in the menu") is None

    def test_empty_and_non_string_results_are_safe(self) -> None:
        assert handoff._handoff_reason("") is None
        assert handoff._handoff_reason("   ") is None
        assert handoff._handoff_reason(None) is None  # type: ignore[arg-type]
        assert handoff._handoff_reason("[1, 2, 3]") is None

    def test_structural_names_skip_body_prose(self) -> None:
        names = handoff._structural_names(
            '- heading "Sign in" [ref=e1]\n'
            '- text "please log in below"\n'
            "- button [ref=e2]: Continue\n"
        )
        assert names == [("heading", "Sign in"), ("button", "Continue")]
