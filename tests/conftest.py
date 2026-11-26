# ABOUTME: Test bootstrap — puts a hermes-agent source tree on sys.path and loads
# ABOUTME: the repo root as the hermes_steel package, mirroring the hermes loader.
"""Shared test setup.

The plugin subclasses ``agent.browser_provider.BrowserProvider`` from the
hermes-agent host, so tests need a hermes-agent source tree importable.
Resolution order:

1. ``HERMES_AGENT_PATH`` environment variable
2. A ``hermes-agent`` checkout sibling to this repository

If neither exists, the test run aborts with instructions — there is no
stubbed fallback; the tests exercise the real host interfaces.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_hermes_agent() -> Path:
    candidates = []
    env = os.environ.get("HERMES_AGENT_PATH", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(REPO_ROOT.parent / "hermes-agent")
    for candidate in candidates:
        if (candidate / "agent" / "browser_provider.py").is_file():
            return candidate
    raise pytest.UsageError(
        "hermes-agent source tree not found. Clone "
        "https://github.com/NousResearch/hermes-agent next to this repo "
        "or point HERMES_AGENT_PATH at a checkout."
    )


HERMES_AGENT_ROOT = _find_hermes_agent()

if str(HERMES_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_ROOT))


def _load_plugin_package() -> None:
    """Import the repo root as the ``hermes_steel`` package.

    The plugin lives at the repository root (so ``hermes plugins install``
    can clone it directly), which means there is no importable package on
    sys.path. Load it the same way the hermes plugin loader does: from
    ``__init__.py`` with the root as the submodule search location.
    """
    if "hermes_steel" in sys.modules:
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hermes_steel",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_steel"] = module
    spec.loader.exec_module(module)


_load_plugin_package()


class FakeResponse:
    """Minimal stand-in for ``requests.Response`` at the HTTP boundary."""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload or {}
        self.text = "" if payload else "error body"

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Clear Steel knobs and other providers' credentials from the ambient env.

    E2E tests re-inject STEEL_API_KEY from the real environment themselves;
    everything else must run hermetically. HERMES_HOME points at an empty
    per-test directory so ``browser.steel.*`` values in the developer's real
    config.yaml can never leak into assertions (tests that need a config
    write their own and re-point HERMES_HOME).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home-isolated"))
    for key in (
        "STEEL_API_KEY",
        "STEEL_BASE_URL",
        "STEEL_USE_PROXY",
        "STEEL_SOLVE_CAPTCHA",
        "STEEL_SESSION_TIMEOUT",
        "STEEL_PROXY_COUNTRY",
        "DATAIMPULSE_PROXY_HOST",
        "DATAIMPULSE_PROXY_PORT",
        "DATAIMPULSE_PROXY_USERNAME",
        "DATAIMPULSE_PROXY_PASSWORD",
        # Cleared so registry-resolution tests can't be perturbed by another
        # provider's credentials in the ambient environment.
        "BROWSERBASE_API_KEY",
        "BROWSERBASE_PROJECT_ID",
        "BROWSER_USE_API_KEY",
        "BROWSER_USE_GATEWAY_URL",
        "FIRECRAWL_API_KEY",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_USER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
