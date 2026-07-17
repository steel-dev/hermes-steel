# ABOUTME: Unit tests for the steel_scrape tool — request shaping, response
# ABOUTME: parsing, SSRF blocking, and error surfaces as JSON strings.
"""Tests for the standalone steel_scrape tool.

Network calls are faked at the ``requests.post`` boundary. The SSRF check
runs the host's real ``tools.url_safety.is_safe_url``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from conftest import FakeResponse
from hermes_steel import scrape_tool
from hermes_steel.scrape_tool import check_steel_scrape_requirements, steel_scrape


def _ok_scrape_payload() -> dict:
    return {
        "content": {"markdown": "# Example Domain\n\nSome text."},
        "metadata": {
            "urlSource": "https://example.com",
            "title": "Example Domain",
            "description": "Illustrative example page",
            "statusCode": 200,
        },
        "links": [{"url": "https://www.iana.org/domains/example", "text": "More"}],
    }


class TestRequirements:
    def test_false_without_api_key(self) -> None:
        assert check_steel_scrape_requirements() is False

    def test_false_with_whitespace_only_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "   ")
        assert check_steel_scrape_requirements() is False

    def test_true_with_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "k")
        assert check_steel_scrape_requirements() is True

    def test_true_with_custom_base_url_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal:3000/")
        assert check_steel_scrape_requirements() is True

    @pytest.mark.parametrize(
        "base_url",
        ["HTTPS://API.STEEL.DEV/", "https://api.steel.dev:443/"],
    )
    def test_false_with_default_base_url_without_api_key(
        self, monkeypatch: pytest.MonkeyPatch, base_url: str
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", base_url)
        assert check_steel_scrape_requirements() is False


class TestSteelScrape:
    def test_happy_path_returns_content_and_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            scrape_tool.requests,
            "post",
            return_value=FakeResponse(200, _ok_scrape_payload()),
        ) as post:
            result = json.loads(steel_scrape("https://example.com"))

        assert result["content"].startswith("# Example Domain")
        assert result["format"] == "markdown"
        assert result["title"] == "Example Domain"
        assert result["status_code"] == 200
        assert result["links_count"] == 1
        body = post.call_args.kwargs["json"]
        assert body == {"url": "https://example.com", "format": ["markdown"]}
        assert post.call_args.kwargs["headers"]["Steel-Api-Key"] == "secret"
        assert post.call_args.kwargs["allow_redirects"] is False

    def test_falls_back_to_available_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {
            "content": {"html": "<h1>hi</h1>"},
            "metadata": {"urlSource": "https://example.com"},
        }
        with patch.object(
            scrape_tool.requests, "post", return_value=FakeResponse(200, payload)
        ):
            result = json.loads(steel_scrape("https://example.com", format="markdown"))
        assert result["content"] == "<h1>hi</h1>"
        assert result["format"] == "html"

    def test_proxy_env_knob_sets_body_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_USE_PROXY", "true")
        with patch.object(
            scrape_tool.requests,
            "post",
            return_value=FakeResponse(200, _ok_scrape_payload()),
        ) as post:
            steel_scrape("https://example.com")
        body = post.call_args.kwargs["json"]
        assert body["useProxy"] is True
        assert "use_proxy" not in body

    def test_proxy_argument_sets_camel_case_body_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            scrape_tool.requests,
            "post",
            return_value=FakeResponse(200, _ok_scrape_payload()),
        ) as post:
            steel_scrape("https://example.com", use_proxy=True)
        assert post.call_args.kwargs["json"]["useProxy"] is True

    def test_normalizes_url_before_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = _ok_scrape_payload()
        payload["metadata"]["urlSource"] = "https://xn--bcher-kva.example/a%20page"
        with (
            patch.object(scrape_tool, "_is_safe_url", return_value=True),
            patch.object(
                scrape_tool.requests,
                "post",
                return_value=FakeResponse(200, payload),
            ) as post,
        ):
            result = json.loads(steel_scrape(" https://bücher.example/a page "))
        assert post.call_args.kwargs["json"]["url"] == (
            "https://xn--bcher-kva.example/a%20page"
        )
        assert result["url"] == "https://xn--bcher-kva.example/a%20page"

    @pytest.mark.parametrize("query_key", ["token", "api_key", "client_secret"])
    def test_sensitive_query_param_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, query_key: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(scrape_tool.requests, "post") as post:
            result = json.loads(
                steel_scrape(f"https://example.com/?{query_key}=opaque-value")
            )
        assert "credential-like query parameter" in result["error"]
        post.assert_not_called()

    def test_known_token_prefix_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(scrape_tool.requests, "post") as post:
            result = json.loads(
                steel_scrape("https://example.com/collect/ghp_abcdefghij1234567890")
            )
        assert "API key or token" in result["error"]
        post.assert_not_called()

    def test_percent_encoded_token_prefix_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(scrape_tool.requests, "post") as post:
            result = json.loads(
                steel_scrape(
                    "https://example.com/collect/sk%2Dant%2Dabcdefghijklmno"
                )
            )
        assert "API key or token" in result["error"]
        post.assert_not_called()

    @pytest.mark.parametrize(
        "url",
        [
            "https://alice:opaque-password@example.com/private",
            "https://opaque-token@example.com/private",
        ],
    )
    def test_url_userinfo_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(scrape_tool.requests, "post") as post:
            result = json.loads(steel_scrape(url))
        assert "embedded user credentials" in result["error"]
        post.assert_not_called()

    def test_website_policy_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        blocked = {
            "host": "blocked.example",
            "rule": "blocked.example",
            "source": "config",
            "message": "Blocked by website policy",
        }
        with (
            patch.object(scrape_tool, "_is_safe_url", return_value=True),
            patch.object(
                scrape_tool, "check_website_access", return_value=blocked
            ),
            patch.object(scrape_tool.requests, "post") as post,
        ):
            result = json.loads(steel_scrape("https://blocked.example/secret"))
        assert result == {
            "error": "Blocked by website policy",
            "blocked_by_policy": {
                "host": "blocked.example",
                "rule": "blocked.example",
                "source": "config",
            },
        }
        post.assert_not_called()

    def test_private_url_blocked_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(scrape_tool.requests, "post") as post:
            result = json.loads(steel_scrape("http://169.254.169.254/latest/meta-data/"))
        assert "Blocked" in result["error"]
        post.assert_not_called()

    def test_unsafe_redirect_discards_scraped_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = _ok_scrape_payload()
        payload["metadata"]["urlSource"] = "http://169.254.169.254/latest/meta-data/"
        with patch.object(
            scrape_tool.requests, "post", return_value=FakeResponse(200, payload)
        ):
            result = json.loads(steel_scrape("https://example.com/redirect"))
        assert result == {
            "error": "Blocked: redirect landed on a private or internal address"
        }
        assert "content" not in result

    def test_policy_blocked_redirect_discards_scraped_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = _ok_scrape_payload()
        payload["metadata"]["urlSource"] = "https://blocked.example/private"
        blocked = {
            "host": "blocked.example",
            "rule": "blocked.example",
            "source": "config",
            "message": "Blocked redirect by website policy",
        }

        def policy(url: str) -> dict[str, str] | None:
            return blocked if url == "https://blocked.example/private" else None

        with (
            patch.object(scrape_tool, "_is_safe_url", return_value=True),
            patch.object(scrape_tool, "check_website_access", side_effect=policy),
            patch.object(
                scrape_tool.requests,
                "post",
                return_value=FakeResponse(200, payload),
            ),
        ):
            result = json.loads(steel_scrape("https://example.com/redirect"))
        assert result["error"] == "Blocked redirect by website policy"
        assert result["blocked_by_policy"]["host"] == "blocked.example"
        assert "content" not in result

    @pytest.mark.parametrize(
        "final_url",
        [
            "https://example.com/callback?token=opaque-value",
            "https://example.com/collect/ghp%5Fabcdefghij1234567890",
            "https://alice:opaque-password@example.com/private",
        ],
    )
    def test_credential_bearing_redirect_discards_scraped_content(
        self, monkeypatch: pytest.MonkeyPatch, final_url: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = _ok_scrape_payload()
        payload["metadata"]["urlSource"] = final_url
        with (
            patch.object(scrape_tool, "_is_safe_url", return_value=True),
            patch.object(
                scrape_tool.requests,
                "post",
                return_value=FakeResponse(200, payload),
            ),
        ):
            result = json.loads(steel_scrape("https://example.com/redirect"))
        assert "Blocked" in result["error"]
        assert "content" not in result
        assert "url" not in result

    @pytest.mark.parametrize(
        "metadata",
        [None, {}, {"urlSource": ""}, {"urlSource": 123}],
    )
    def test_missing_or_invalid_final_url_discards_scraped_content(
        self, monkeypatch: pytest.MonkeyPatch, metadata: object
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = {
            "content": {"markdown": "private redirect content"},
            "metadata": metadata,
        }
        with patch.object(
            scrape_tool.requests,
            "post",
            return_value=FakeResponse(200, payload),
        ):
            result = json.loads(steel_scrape("https://example.com/redirect"))
        assert result == {
            "error": "Steel scrape response did not include a verifiable final URL"
        }
        assert "private redirect content" not in json.dumps(result)

    def test_public_redirect_returns_final_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        payload = _ok_scrape_payload()
        payload["metadata"]["urlSource"] = "https://www.iana.org/help/example-domains"
        with patch.object(
            scrape_tool.requests, "post", return_value=FakeResponse(200, payload)
        ):
            result = json.loads(steel_scrape("https://example.com/redirect"))
        assert result["url"] == "https://www.iana.org/help/example-domains"
        assert result["content"].startswith("# Example Domain")

    def test_custom_base_url_does_not_require_or_send_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_BASE_URL", "http://steel.internal:3000/")
        with (
            patch.object(scrape_tool, "_is_safe_url", return_value=True),
            patch.object(
                scrape_tool.requests,
                "post",
                return_value=FakeResponse(200, _ok_scrape_payload()),
            ) as post,
        ):
            result = json.loads(steel_scrape("https://example.com"))
        assert "error" not in result
        assert post.call_args.args[0] == "http://steel.internal:3000/v1/scrape"
        assert "Steel-Api-Key" not in post.call_args.kwargs["headers"]

    @pytest.mark.parametrize("configured_base", ["", "   "])
    def test_blank_base_url_uses_default_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, configured_base: str
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        monkeypatch.setenv("STEEL_BASE_URL", configured_base)
        with patch.object(
            scrape_tool.requests,
            "post",
            return_value=FakeResponse(200, _ok_scrape_payload()),
        ) as post:
            result = json.loads(steel_scrape("https://example.com"))
        assert "error" not in result
        assert post.call_args.args[0] == "https://api.steel.dev/v1/scrape"

    def test_missing_api_key_returns_error_json(self) -> None:
        result = json.loads(steel_scrape("https://example.com"))
        assert result["error"] == "STEEL_API_KEY not configured"

    def test_http_error_returns_error_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        response = FakeResponse(500, None)
        response.text = "private redirect content and upstream diagnostics"
        with patch.object(
            scrape_tool.requests, "post", return_value=response
        ):
            result = json.loads(steel_scrape("https://example.com"))
        assert result == {"error": "Steel scrape failed with HTTP 500"}
        assert "private redirect content" not in json.dumps(result)

    def test_api_redirect_is_not_followed_or_treated_as_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        response = FakeResponse(302, None)
        response.text = "redirecting to credential collector"
        with patch.object(
            scrape_tool.requests, "post", return_value=response
        ) as post:
            result = json.loads(steel_scrape("https://example.com"))
        assert result == {"error": "Steel scrape failed with HTTP 302"}
        assert post.call_args.kwargs["allow_redirects"] is False
        assert "credential collector" not in json.dumps(result)

    def test_network_exception_returns_error_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STEEL_API_KEY", "secret")
        with patch.object(
            scrape_tool.requests, "post", side_effect=OSError("connection reset")
        ):
            result = json.loads(steel_scrape("https://example.com"))
        assert result == {"error": "Steel scrape request failed"}
        assert "connection reset" not in json.dumps(result)


class TestSchema:
    def test_schema_shape(self) -> None:
        schema = scrape_tool.STEEL_SCRAPE_SCHEMA
        assert schema["name"] == "steel_scrape"
        assert schema["parameters"]["required"] == ["url"]
        assert set(schema["parameters"]["properties"]) == {"url", "format"}
