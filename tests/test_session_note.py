# ABOUTME: Unit tests for the transform_tool_result hook that tags the first
# ABOUTME: browser result of each Steel session with a user-facing note.
"""Tests for the Steel session note hook.

The hook appends a one-line note (backend + live-viewer link) to the first
``browser_*`` tool result of each Steel cloud session, so the agent can tell
the user their browsing runs on Steel. It must stay silent for non-Steel
providers, non-browser tools, and every subsequent call in the same session.
"""

from __future__ import annotations

import pytest

from hermes_steel import session_note
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


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the noted-session tracking and the browser_tool surface."""
    from tools import browser_tool

    monkeypatch.setattr(session_note, "_noted_session_ids", set())
    monkeypatch.setattr(session_note, "_hinted_session_ids", set())
    monkeypatch.setattr(browser_tool, "_active_sessions", {}, raising=True)
    monkeypatch.setattr(
        session_note, "_get_cloud_provider", lambda: SteelBrowserProvider()
    )
    return browser_tool


class TestSteelSessionNote:
    def test_host_invocation_contract(self, _fresh_state) -> None:
        """Exactly the keyword set model_tools.py passes to the hook.

        The host sends ``args=`` (not ``arguments=``, despite the hooks doc),
        empty strings instead of None for missing IDs, and a tail of
        telemetry kwargs the callback must swallow.
        """
        _fresh_state._active_sessions["default"] = _steel_session()
        out = steel_session_note(
            tool_name="browser_navigate",
            args={"url": "https://example.com"},
            result='{"success": true}',
            task_id="",
            session_id="20260716_153039_6423fe",
            tool_call_id="call_1",
            turn_id="turn_1",
            api_request_id="req_1",
            duration_ms=2300,
            status="success",
            error_type="",
            error_message="",
        )
        assert out is not None
        assert "Steel" in out

    def test_first_browser_result_gets_note_with_viewer_link(
        self, _fresh_state
    ) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        out = steel_session_note(
            tool_name="browser_navigate",
            args={"url": "https://example.com"},
            result='{"success": true}',
            task_id=None,
        )
        assert out is not None
        assert out.startswith('{"success": true}')
        assert "Steel" in out
        assert "https://app.steel.dev/sessions/sess-1" in out

    def test_same_session_is_noted_only_once(self, _fresh_state) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        first = steel_session_note(
            tool_name="browser_navigate",
            args={},
            result="r1",
            task_id=None,
        )
        second = steel_session_note(
            tool_name="browser_snapshot",
            args={},
            result="r2",
            task_id=None,
        )
        assert first is not None
        assert second is None

    def test_new_session_in_same_task_is_noted_again(self, _fresh_state) -> None:
        _fresh_state._active_sessions["default"] = _steel_session("sess-1")
        assert steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        ) is not None
        _fresh_state._active_sessions["default"] = _steel_session("sess-2")
        out = steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        )
        assert out is not None
        assert "sess-2" in out

    def test_task_scoped_sessions_resolve_by_task_id(self, _fresh_state) -> None:
        _fresh_state._active_sessions["task-9"] = _steel_session()
        out = steel_session_note(
            tool_name="browser_click", args={}, result="r", task_id="task-9"
        )
        assert out is not None

    def test_ignores_non_browser_tools(self, _fresh_state) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        assert steel_session_note(
            tool_name="steel_scrape", args={}, result="r", task_id=None
        ) is None
        assert steel_session_note(
            tool_name="read_file", args={}, result="r", task_id=None
        ) is None

    def test_deferred_past_error_results_until_first_success(
        self, _fresh_state
    ) -> None:
        """A model mid-error-recovery ignores side notes, and error results
        may be retried — hold the note until a result the model will act on."""
        _fresh_state._active_sessions["default"] = _steel_session()
        on_error = steel_session_note(
            tool_name="browser_navigate",
            args={},
            result='{"error": "Navigation timeout"}',
            task_id=None,
            status="error",
        )
        assert on_error is None
        on_success = steel_session_note(
            tool_name="browser_snapshot",
            args={},
            result='{"success": true}',
            task_id=None,
            status="ok",
        )
        assert on_success is not None
        assert "Steel" in on_success

    def test_degraded_session_note_explains_how_to_unlock(
        self, _fresh_state
    ) -> None:
        session = _steel_session()
        session["degraded_features"] = ["proxy", "captcha_solving"]
        session["degradation_reason"] = "Steel proxies are not available on the hobby plan."
        _fresh_state._active_sessions["default"] = session
        out = steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        )
        assert out is not None
        assert "without" in out and "proxy" in out
        assert "app.steel.dev/settings/billing" in out

    def test_first_note_teaches_block_remedy_when_features_off(
        self, _fresh_state
    ) -> None:
        """The model detects bot walls on its own; what it lacks is remedy
        knowledge. Teach it up front so no keyword matching is needed."""
        _fresh_state._active_sessions["default"] = _steel_session()
        out = steel_session_note(
            tool_name="browser_navigate", args={}, result="ok", task_id=None
        )
        assert out is not None
        assert "steel_session_options" in out
        assert "use_proxy" in out

    def test_first_note_omits_remedy_when_features_enabled(
        self, _fresh_state
    ) -> None:
        session = _steel_session()
        session["features"] = {"proxy": True, "captcha_solving": True}
        _fresh_state._active_sessions["default"] = session
        out = steel_session_note(
            tool_name="browser_navigate", args={}, result="ok", task_id=None
        )
        assert out is not None
        assert "steel_session_options" not in out

    def test_bot_block_hint_suggests_knobs_once_per_session(
        self, _fresh_state
    ) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        blocked = '{"success": true, "snapshot": "Verify you are human ..."}'
        first = steel_session_note(
            tool_name="browser_snapshot", args={}, result=blocked, task_id=None
        )
        assert first is not None
        assert "steel_session_options" in first
        assert "use_proxy" in first and "solve_captcha" in first
        repeat = steel_session_note(
            tool_name="browser_snapshot", args={}, result=blocked, task_id=None
        )
        assert repeat is None

    def test_bot_block_hint_on_degraded_session_points_at_verification(
        self, _fresh_state
    ) -> None:
        session = _steel_session()
        session["degraded_features"] = ["proxy"]
        _fresh_state._active_sessions["default"] = session
        # Consume the first-result note first.
        steel_session_note(
            tool_name="browser_navigate", args={}, result="ok", task_id=None
        )
        out = steel_session_note(
            tool_name="browser_snapshot",
            args={},
            result="Just a moment...",
            task_id=None,
        )
        assert out is not None
        assert "app.steel.dev/settings/billing" in out
        assert "steel_session_options" not in out

    def test_no_bot_block_hint_when_features_already_enabled(
        self, _fresh_state
    ) -> None:
        session = _steel_session()
        session["features"] = {"proxy": True, "captcha_solving": True}
        _fresh_state._active_sessions["default"] = session
        steel_session_note(
            tool_name="browser_navigate", args={}, result="ok", task_id=None
        )
        out = steel_session_note(
            tool_name="browser_snapshot",
            args={},
            result="Verify you are human",
            task_id=None,
        )
        assert out is None

    @pytest.mark.parametrize(
        "block_text",
        [
            # PerimeterX / HUMAN default block page (Ticketmaster et al.)
            "Access to this page has been denied.",
            # Ticketmaster interstitial
            "Pardon the Interruption",
            "We noticed some unusual activity from your browser",
            # Cloudflare challenge
            "Checking if the site connection is secure",
        ],
    )
    def test_real_world_block_pages_trigger_hint(
        self, _fresh_state, block_text: str
    ) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        out = steel_session_note(
            tool_name="browser_navigate",
            args={},
            result=f'{{"success": true, "snapshot": "{block_text}"}}',
            task_id=None,
        )
        assert out is not None
        assert "bot-protected" in out

    def test_ordinary_content_never_triggers_block_hint(
        self, _fresh_state
    ) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        steel_session_note(
            tool_name="browser_navigate", args={}, result="ok", task_id=None
        )
        out = steel_session_note(
            tool_name="browser_snapshot",
            args={},
            result='{"snapshot": "Welcome to Example Domain, a human-friendly page"}',
            task_id=None,
        )
        assert out is None

    def test_silent_when_session_fell_back_to_local(self, _fresh_state) -> None:
        """A cloud-create failure falls back to local Chromium — the session
        is not on Steel, so claiming it is would misinform the user."""
        session = _steel_session()
        session["fallback_from_cloud"] = True
        session["fallback_reason"] = "STEEL_API_KEY invalid"
        _fresh_state._active_sessions["default"] = session
        assert steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        ) is None

    def test_applied_note_is_logged_for_diagnosis(
        self, _fresh_state, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        with caplog.at_level("INFO", logger="hermes_steel.session_note"):
            steel_session_note(
                tool_name="browser_navigate", args={}, result="r", task_id=None
            )
        assert any("Steel session note applied" in r.message for r in caplog.records)

    def test_silent_when_no_active_session(self, _fresh_state) -> None:
        assert steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        ) is None

    def test_silent_when_provider_is_not_steel(
        self, _fresh_state, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_state._active_sessions["default"] = _steel_session()
        monkeypatch.setattr(session_note, "_get_cloud_provider", lambda: None)
        assert steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        ) is None

    def test_note_without_viewer_url_still_names_steel(self, _fresh_state) -> None:
        session = _steel_session()
        del session["session_viewer_url"]
        _fresh_state._active_sessions["default"] = session
        out = steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        )
        assert out is not None
        assert "Steel" in out
        assert "app.steel.dev/sessions" not in out

    def test_never_raises_when_browser_tool_surface_changes(
        self, _fresh_state, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom():
            raise RuntimeError("host internals moved")

        monkeypatch.setattr(session_note, "_get_cloud_provider", _boom)
        assert steel_session_note(
            tool_name="browser_navigate", args={}, result="r", task_id=None
        ) is None
