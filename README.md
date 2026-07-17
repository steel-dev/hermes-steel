# hermes-steel

![Hermes raising the caduceus over a storm-lit agora](assets/hermes.png)

Give [Hermes Agent](https://github.com/NousResearch/hermes-agent) a cloud browser: this plugin routes Hermes' browser automation through [Steel](https://steel.dev), with a live viewer for every session and a standalone `steel_scrape` tool for one-shot page extraction.

## Quickstart

You need [Hermes Agent](https://github.com/NousResearch/hermes-agent) 0.18.0+ and a [Steel account](https://app.steel.dev) — the free plan is fine.

```bash
hermes plugins install steel-dev/hermes-steel --enable
hermes config set browser.cloud_provider steel
```

The installer prompts for your Steel API key ([get one here](https://app.steel.dev/settings/api-keys)) and saves it to `~/.hermes/.env`. The second command selects Steel as the browser backend — **don't skip it**: Steel is never auto-selected, so without it Hermes keeps browsing on a local Chromium even with the plugin installed and the key set.

Done. Ask Hermes to browse something:

```
❯ navigate to news.ycombinator.com and summarize the front page
```

Its first report will confirm the session runs on Steel and include a live-viewer link (`https://app.steel.dev/sessions/<id>`) so you can watch it work in real time.

> [!NOTE]
> **Works out of the box on Steel's free plan.** Residential proxies and CAPTCHA solving additionally require a verified Launch account — verify by adding $10 in paid balance (free credits don't count; the deposit goes toward usage). See [pricing & limits](https://docs.steel.dev/overview/pricinglimits).
>
> Enabling these on a free account is safe: if Steel declines them, the plugin creates the session without them and the agent tells you what was dropped and how to unlock it.

## What you get

- **Cloud browser automation** — every `browser_*` tool (`browser_navigate`, `browser_click`, `browser_screenshot`, …) runs in a Steel session, with optional residential proxies and CAPTCHA solving.
- **`steel_scrape`** — server-side page extraction to clean markdown or HTML via Steel's `/v1/scrape` API. No browser session needed, and it works with just the API key (no `cloud_provider` change).
- **An agent that knows its backend** — Hermes' browser tools are provider-agnostic, so the plugin tags the first browser result of each session with the backend name and viewer link. The agent tells you it's browsing on Steel instead of guessing about its own infrastructure.
- **Self-tuning anti-bot** — the agent can call `steel_session_options` to toggle residential proxy and CAPTCHA solving per site and start a fresh session, deciding for itself when a page needs them (and when it works better without). No config edit or restart; it reacts to what it sees.

## Configuration

`STEEL_API_KEY` is the only required setting for Steel cloud (it lives in `~/.hermes/.env` — secrets stay out of config.yaml). Everything else is set the hermes-native way:

```bash
hermes config set browser.steel.use_proxy true
hermes config set browser.steel.solve_captcha true
```

| Config key (`hermes config set …`) | Env override | Default | Effect |
|---|---|---|---|
| `browser.steel.base_url` | `STEEL_BASE_URL` | `https://api.steel.dev` | Steel API endpoint; a custom URL enables self-hosted mode |
| `browser.steel.use_proxy` | `STEEL_USE_PROXY` | `false` | Residential proxy for sessions and scrapes |
| `browser.steel.solve_captcha` | `STEEL_SOLVE_CAPTCHA` | `false` | CAPTCHA solving |
| `browser.steel.session_timeout` | `STEEL_SESSION_TIMEOUT` | `1800000` (30 min) | Session hard cap in milliseconds |

The 30-minute default replaces Steel's own 5-minute one, which kills long agent tasks mid-flight; the plugin also sets a 10-minute server-side inactivity timeout as crash insurance.

Precedence is **agent override → env var → config → default**: the agent's per-session choices (via `steel_session_options`) win, then `STEEL_*` env vars, then `browser.steel.*` config. So config sets your persistent baseline while the agent tunes individual sessions on top of it.

**Self-hosted Steel:** point `STEEL_BASE_URL` at your deployment. The API key becomes optional — when set, it is forwarded; when not, requests carry no credentials. `steel_scrape` follows the same rules.

## Managing the plugin

```bash
hermes plugins update browser-steel     # pull the latest version
hermes plugins remove browser-steel     # uninstall
```

When removing, also switch the backend selection back — otherwise Hermes warns about the missing Steel provider on every browse:

```bash
hermes config set browser.cloud_provider local
```

## Troubleshooting

- **The agent never mentions Steel / browses locally** — you skipped `hermes config set browser.cloud_provider steel` (see Quickstart). Steel is never auto-selected.
- **The agent reports proxy/CAPTCHA "were dropped"** — your Steel account isn't verified; unlock them at [billing](https://app.steel.dev/settings/billing).
- **Still blocked by a bot wall** — the agent retries with proxy/CAPTCHA via `steel_session_options` on its own, but on an unverified account those features silently run disabled, so hard anti-bot sites stay out of reach until you verify.
- **Not sure it's working?** — the agent's first browse report includes the live-viewer link; open it and watch the session.

## How it works

Steel exposes a standard CDP websocket per session. The provider creates a session via `POST /v1/sessions`, normalizes the returned `websocketUrl` (Steel omits the `/` path segment, which makes some CDP clients send a malformed request-line and get `400 Bad Request` on the websocket upgrade), and hands the URL to Hermes' provider-agnostic browser stack. The API key is appended only when configured, so unauthenticated self-hosted endpoints receive no auth header or query parameter. No Steel CLI, no separate Python packaging — the plugin runs inside Hermes' own environment. Browsing runs on Steel's infrastructure; the API key stays in `~/.hermes/.env` and the plugin sends nothing anywhere except the Steel API you configured.

## Development

Symlink a working copy so edits apply without reinstalling:

```bash
git clone https://github.com/steel-dev/hermes-steel
ln -s "$(pwd)/hermes-steel" ~/.hermes/plugins/browser-steel
hermes plugins enable browser-steel
```

Tests need a hermes-agent source tree — clone it as a sibling directory or set `HERMES_AGENT_PATH`:

```bash
git clone https://github.com/NousResearch/hermes-agent ../hermes-agent
uv run --project ../hermes-agent --extra dev pytest
```

Three test layers: unit (HTTP boundary faked), integration (the plugin loaded through Hermes' real `PluginManager`, plus the hook invoked with the host's exact kwargs), and e2e against the live Steel API (auto-skip unless `STEEL_API_KEY` is set). CI additionally runs the real `hermes plugins install` against the checkout, and a weekly secret-gated workflow drives the live browser path through Hermes and `agent-browser`.

## Provenance

Extracted from [NousResearch/hermes-agent#21182](https://github.com/NousResearch/hermes-agent/pull/21182), closed per the maintainers' standing policy that third-party vendor integrations ship as standalone plugin repos.
