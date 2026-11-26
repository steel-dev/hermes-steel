# ABOUTME: Plugin entry point — registers the Steel browser provider and the
# ABOUTME: steel_scrape tool with the Hermes Agent plugin context.
"""Steel cloud browser plugin for Hermes Agent.

Standalone plugin (not bundled with hermes-agent core, per the project's
third-party-provider policy). The repository root is the plugin package —
install with ``hermes plugins install steel-dev/hermes-steel --enable``,
which clones it into ``~/.hermes/plugins/browser-steel/``.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the Steel browser provider, tools, and hooks."""
    from .provider import SteelBrowserProvider
    from . import scrape_tool
    from . import session_note
    from . import session_options

    ctx.register_browser_provider(SteelBrowserProvider())

    # Tag the first browser_* result of each Steel session with the backend
    # name and live-viewer URL so the agent can pass both on to the user.
    ctx.register_hook("transform_tool_result", session_note.steel_session_note)

    ctx.register_tool(
        name="steel_session_options",
        toolset="browser",
        schema=session_options.STEEL_SESSION_OPTIONS_SCHEMA,
        handler=lambda args, **kw: session_options.steel_session_options(
            use_proxy=args.get("use_proxy"),
            proxy_country=args.get("proxy_country"),
            solve_captcha=args.get("solve_captcha"),
            task_id=kw.get("task_id"),
        ),
        check_fn=scrape_tool.check_steel_scrape_requirements,
        requires_env=[],
        description=(
            "Tune the Steel session's Dataimpulse proxy / country / CAPTCHA "
            "solving for the current task, then start a fresh session"
        ),
    )

    ctx.register_tool(
        name="steel_scrape",
        toolset="browser",
        schema=scrape_tool.STEEL_SCRAPE_SCHEMA,
        handler=lambda args, **kw: scrape_tool.steel_scrape(
            url=args.get("url", ""),
            format=args.get("format", "markdown"),
        ),
        check_fn=scrape_tool.check_steel_scrape_requirements,
        # Cloud Steel requires a key, while a custom STEEL_BASE_URL may be an
        # unauthenticated self-hosted deployment.  The dynamic check_fn above
        # enforces the right requirement for the selected endpoint.
        requires_env=[],
        description=(
            "Server-side webpage extraction as markdown/HTML via Steel's "
            "scrape API — no browser session required"
        ),
    )
