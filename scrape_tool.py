# ABOUTME: Steel scrape tool for server-side content extraction as markdown/HTML.
# ABOUTME: Uses Steel's standalone scrape API — no browser session required.
"""Steel Scrape Tool — extract page content via Steel's server-side scrape API.

Unlike browser_navigate + browser_snapshot which use a full browser session and
accessibility tree, this tool extracts content server-side as clean markdown or
HTML.  It does not require an active browser session and is useful for quick
content extraction without interactive browsing.

Registration happens in :func:`hermes_steel.register` via
``ctx.register_tool`` — this module holds only the handler and schema.

Environment Variables:
- STEEL_API_KEY: Required for Steel Cloud; optional for a custom self-hosted endpoint
- STEEL_BASE_URL: Optional — custom self-hosted Steel endpoint
- STEEL_USE_PROXY: Optional — enable residential proxy for scraping
  (or the hermes-native `browser.steel.use_proxy` config key)
"""

import json
import logging
import os
from typing import Optional
from urllib.parse import unquote, urlsplit

import requests
from agent.redact import _PREFIX_RE
from tools.url_safety import (
    is_safe_url as _is_safe_url,
    normalize_url_for_request as _normalize_url_for_request,
    sensitive_query_param_name as _sensitive_query_param_name,
)
from tools.website_policy import check_website_access

from .config import (
    custom_base_url,
    is_default_cloud_url,
    steel_api_key,
    steel_base_url,
    steel_setting,
)

logger = logging.getLogger(__name__)


def check_steel_scrape_requirements() -> bool:
    """Return True when hosted or self-hosted Steel is configured."""
    return bool(steel_api_key() or custom_base_url())


def _policy_error(blocked: dict[str, str]) -> str:
    """Return a policy denial without exposing content from the blocked page."""
    result: dict[str, object] = {
        "error": blocked.get("message", "Blocked by website policy"),
    }
    policy_details = {
        key: blocked[key]
        for key in ("host", "rule", "source")
        if key in blocked
    }
    if policy_details:
        result["blocked_by_policy"] = policy_details
    return json.dumps(result)


def _credential_error(url: str) -> Optional[str]:
    """Reject credential-bearing URLs before they reach the Steel service."""
    if not isinstance(url, str):
        return json.dumps({"error": "Invalid URL: expected a string"})

    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        parsed = None
    if parsed is not None and (
        parsed.username is not None or parsed.password is not None
    ):
        return json.dumps({
            "error": "Blocked: URL contains embedded user credentials. "
            "Secrets must not be sent in URLs.",
        })

    decoded_url = unquote(url)
    if _PREFIX_RE.search(url) or _PREFIX_RE.search(decoded_url):
        return json.dumps({
            "error": "Blocked: URL contains what appears to be an API key or token. "
            "Secrets must not be sent in URLs.",
        })

    sensitive_query_key = _sensitive_query_param_name(url)
    if sensitive_query_key:
        return json.dumps({
            "error": (
                "Blocked: URL contains a credential-like query parameter "
                f"({sensitive_query_key}). Remove it before scraping."
            ),
        })
    return None


def steel_scrape(
    url: str,
    format: str = "markdown",
    use_proxy: Optional[bool] = None,
) -> str:
    """Scrape a URL via Steel's server-side API and return extracted content.

    Args:
        url: The URL to scrape.
        format: Output format — "markdown", "html", "readability", or "cleaned_html".
        use_proxy: Override proxy setting (defaults to the STEEL_USE_PROXY
            env var, falling back to the browser.steel.use_proxy config key).

    Returns:
        JSON string with extracted content, metadata, and links.
    """
    credential_error = _credential_error(url)
    if credential_error:
        return credential_error

    url = _normalize_url_for_request(url)
    credential_error = _credential_error(url)
    if credential_error:
        return credential_error

    if not _is_safe_url(url):
        return json.dumps({
            "error": "Blocked: URL targets a private or internal address",
        })

    blocked = check_website_access(url)
    if blocked:
        return _policy_error(blocked)

    api_key = steel_api_key()
    base = steel_base_url()
    if not api_key and is_default_cloud_url(base):
        return json.dumps({"error": "STEEL_API_KEY not configured"})

    body = {
        "url": url,
        "format": [format],
    }

    if use_proxy is None:
        use_proxy = (steel_setting("STEEL_USE_PROXY", "use_proxy") or "").lower() in (
            "1", "true", "yes",
        )
    if use_proxy:
        body["useProxy"] = True

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Steel-Api-Key"] = api_key

    try:
        response = requests.post(
            f"{base}/v1/scrape",
            headers=headers,
            json=body,
            timeout=60,
            allow_redirects=False,
        )
    except Exception as exc:
        logger.warning(
            "Steel scrape request failed (%s)",
            type(exc).__name__,
        )
        return json.dumps({"error": "Steel scrape request failed"})

    if not 200 <= response.status_code < 300:
        logger.warning(
            "Steel scrape request returned HTTP %s",
            response.status_code,
        )
        return json.dumps({
            "error": f"Steel scrape failed with HTTP {response.status_code}",
        })

    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("response body was not an object")

        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            return json.dumps({
                "error": "Steel scrape response did not include a verifiable final URL",
            })

        source_url = metadata.get("urlSource")
        if not isinstance(source_url, str) or not source_url.strip():
            return json.dumps({
                "error": "Steel scrape response did not include a verifiable final URL",
            })

        credential_error = _credential_error(source_url)
        if credential_error:
            return credential_error
        final_url = _normalize_url_for_request(source_url)

        credential_error = _credential_error(final_url)
        if credential_error:
            return credential_error

        if not _is_safe_url(final_url):
            return json.dumps({
                "error": "Blocked: redirect landed on a private or internal address",
            })

        blocked = check_website_access(final_url)
        if blocked:
            return _policy_error(blocked)

        result = {
            "url": final_url,
            "format": format,
        }

        # Extract content — try requested format first, then fall back
        content = data.get("content", {})
        if content.get(format):
            result["content"] = content[format]
        else:
            for key in ("markdown", "readability", "cleaned_html", "html"):
                if content.get(key):
                    result["content"] = content[key]
                    result["format"] = key
                    break

        if metadata.get("title"):
            result["title"] = metadata["title"]
        if metadata.get("description"):
            result["description"] = metadata["description"]
        if metadata.get("statusCode"):
            result["status_code"] = metadata["statusCode"]

        links = data.get("links", [])
        if links:
            result["links_count"] = len(links)
            result["links"] = links[:20]

        return json.dumps(result, ensure_ascii=False)

    except Exception as exc:
        logger.warning(
            "Steel scrape returned an invalid response (%s)",
            type(exc).__name__,
        )
        return json.dumps({"error": "Steel scrape returned an invalid response"})


# --- Schema ---

STEEL_SCRAPE_SCHEMA = {
    "name": "steel_scrape",
    "description": (
        "Extract content from a webpage using Steel's server-side scrape API. "
        "Returns clean markdown or HTML without needing a browser session. "
        "Best for quick content extraction; use browser_navigate for interactive browsing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of the webpage to scrape",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "html", "readability", "cleaned_html"],
                "description": "Output format (default: markdown)",
                "default": "markdown",
            },
        },
        "required": ["url"],
    },
}
