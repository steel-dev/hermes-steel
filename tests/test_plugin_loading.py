# ABOUTME: Integration test — loads this plugin through the real hermes-agent
# ABOUTME: PluginManager and asserts provider + tool registration end to end.
"""Integration: hermes-agent discovers and loads the plugin from disk.

Builds a throwaway HERMES_HOME with this repository symlinked to
``plugins/browser-steel/`` — the same flat layout ``hermes plugins install``
produces — and ``plugins.enabled`` opting it in, then runs the host's real
discovery sweep and checks both registration surfaces (browser provider
registry and tool registry).
"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT


class TestPluginDiscovery:
    def test_discovery_registers_provider_and_tool(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hermes_home = tmp_path / "hermes-home"
        plugins_dir = hermes_home / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "browser-steel").symlink_to(REPO_ROOT)
        (hermes_home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - browser-steel\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        from hermes_cli.plugins import get_plugin_manager

        manager = get_plugin_manager()
        manager.discover_and_load(force=True)

        loaded = {
            key: plugin
            for key, plugin in manager._plugins.items()
            if "steel" in key
        }
        assert "browser-steel" in loaded, f"steel plugin not discovered: {loaded}"
        assert loaded["browser-steel"].enabled, loaded["browser-steel"].error

        # The session-note hook must be registered so the agent can tell the
        # user their browsing runs on Steel.
        assert manager.has_hook("transform_tool_result")

        # Invoke it through the host dispatcher with the exact kwargs
        # model_tools.py sends. A callback signature mismatch is swallowed
        # fail-open in production (the note silently never fires), so assert
        # the registered callback actually returns the amended result.
        from hermes_cli.plugins import invoke_hook
        from tools import browser_tool

        from hermes_steel.provider import SteelBrowserProvider

        monkeypatch.setattr(
            browser_tool,
            "_active_sessions",
            {
                "default": {
                    "session_name": "hermes_int_1",
                    "bb_session_id": "sess-int-1",
                    "cdp_url": "wss://connect.steel.dev/?sessionId=sess-int-1",
                    "features": {},
                    "session_viewer_url": "https://app.steel.dev/sessions/sess-int-1",
                }
            },
        )
        try:
            from tools import browser_tool_cloud

            monkeypatch.setattr(
                browser_tool_cloud,
                "_get_cloud_provider",
                lambda: SteelBrowserProvider(),
            )
        except ImportError:
            monkeypatch.setattr(
                browser_tool, "_get_cloud_provider", lambda: SteelBrowserProvider()
            )
        hook_results = invoke_hook(
            "transform_tool_result",
            tool_name="browser_navigate",
            args={"url": "https://example.com"},
            result='{"success": true}',
            task_id="",
            session_id="sess",
            tool_call_id="call_1",
            turn_id="turn_1",
            api_request_id="req_1",
            duration_ms=1,
            status="success",
            error_type="",
            error_message="",
        )
        amended = [r for r in hook_results if isinstance(r, str)]
        assert amended and "Steel" in amended[0], hook_results

        # `hermes plugins install` prompts for these at install time. The key
        # is skippable so unauthenticated self-hosted deployments still work.
        manifest_env = loaded["browser-steel"].manifest.requires_env
        assert len(manifest_env) == 1, manifest_env
        key_spec = manifest_env[0]
        assert key_spec["name"] == "STEEL_API_KEY", key_spec
        assert key_spec["secret"] is True, key_spec
        assert key_spec.get("url"), key_spec

        from agent.browser_registry import _resolve

        provider = _resolve("steel")
        assert provider is not None
        assert provider.name == "steel"
        assert provider.display_name == "Steel"

        from tools.registry import registry

        entry = registry.get_entry("steel_scrape")
        assert entry is not None
        assert entry.toolset == "browser"
        # Hosted Steel requires STEEL_API_KEY, but self-hosted endpoints may
        # be unauthenticated. The tool's dynamic check_fn enforces whichever
        # requirement applies to the configured endpoint.
        assert entry.requires_env == []
