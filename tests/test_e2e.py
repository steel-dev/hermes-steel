# ABOUTME: End-to-end tests against the real Steel API — Hermes/agent-browser
# ABOUTME: navigation plus server-side scrape. Skip when STEEL_API_KEY is unset.
"""E2E: real Steel API calls, gated on ``STEEL_API_KEY``.

The key is captured at import time because the hermetic ``_isolate_env``
fixture strips it from the environment before each test; these tests
re-inject it deliberately.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from hermes_steel.provider import SteelBrowserProvider
from hermes_steel.scrape_tool import steel_scrape

_API_KEY = os.environ.get("STEEL_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not _API_KEY, reason="STEEL_API_KEY not set — skipping live Steel API tests"
)


@pytest.fixture()
def _live_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEEL_API_KEY", _API_KEY)


class TestLiveBrowserAutomation:
    def test_navigate_and_snapshot_through_hermes_agent_browser(
        self, _live_key: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercise the production Hermes dispatcher, not just URL shaping."""
        from tools import browser_tool

        provider = SteelBrowserProvider()
        task_id = f"steel-e2e-{uuid.uuid4().hex[:8]}"
        release_results: list[bool] = []
        close_session = provider.close_session
        body_error: BaseException | None = None

        def close_and_record(session_id: str) -> bool:
            released = close_session(session_id)
            release_results.append(released)
            return released

        monkeypatch.setattr(provider, "close_session", close_and_record)
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)

        try:
            navigation = json.loads(
                browser_tool.browser_navigate(
                    "https://example.com/", task_id=task_id
                )
            )
            assert navigation.get("success") is True, navigation
            assert navigation.get("title") == "Example Domain", navigation

            session = browser_tool._active_sessions[task_id]
            assert str(session["cdp_url"]).startswith("wss://")
            assert "apiKey=" in str(session["cdp_url"])
            assert session["bb_session_id"]

            snapshot = json.loads(
                browser_tool.browser_snapshot(full=True, task_id=task_id)
            )
            assert snapshot.get("success") is True, snapshot
            assert "Example Domain" in snapshot.get("snapshot", ""), snapshot
        except BaseException as exc:
            # Retain the navigation/snapshot failure as the primary exception.
            # The finally block can attach cleanup diagnostics without masking it.
            body_error = exc
            raise
        finally:
            session = browser_tool._active_sessions.get(task_id, {})
            session_id = session.get("bb_session_id")
            cleanup_error: BaseException | None = None

            # Hermes closes agent-browser first and then releases the Steel
            # session through the provider. Keep this in finally so navigation
            # failures still trigger release attempts.
            try:
                browser_tool.cleanup_browser(task_id)
            except BaseException as exc:
                cleanup_error = exc

            normal_release_failed = bool(session_id) and release_results != [True]
            still_tracked = task_id in browser_tool._active_sessions
            cleanup_failed = bool(cleanup_error or normal_release_failed or still_tracked)

            fallback_released: bool | None = None
            fallback_error: BaseException | None = None
            if cleanup_failed and session_id:
                # One direct fallback prevents a failed Hermes cleanup path from
                # leaving a billable cloud session running. close_session itself
                # performs its normal bounded retry policy.
                try:
                    fallback_released = close_session(str(session_id))
                except BaseException as exc:
                    fallback_error = exc
                    fallback_released = False

            if cleanup_failed:
                message = (
                    "Steel E2E cleanup failed: "
                    f"release_results={release_results!r}, "
                    f"still_tracked={still_tracked}, "
                    f"fallback_released={fallback_released!r}"
                )
                if body_error is not None:
                    body_error.add_note(message)
                    if cleanup_error is not None:
                        body_error.add_note(f"Hermes cleanup error: {cleanup_error!r}")
                    if fallback_error is not None:
                        body_error.add_note(
                            f"Direct fallback release error: {fallback_error!r}"
                        )
                else:
                    cause = cleanup_error or fallback_error
                    if cause is not None:
                        raise AssertionError(message) from cause
                    raise AssertionError(message)


class TestLiveScrape:
    def test_scrape_example_dot_com(self, _live_key: None) -> None:
        result = json.loads(steel_scrape("https://example.com"))
        assert "error" not in result, result
        assert result.get("title") == "Example Domain"
        assert "documentation examples" in result["content"]
