# SPDX-License-Identifier: Apache-2.0
"""Behaviour pinned against the public docs and the gateway's real handling."""

import httpx
import pytest
import respx

from sdo import Client, ScrapeConfig, Usage
from sdo.errors import (
    SdoConcurrencyError,
    SdoConfigError,
    SdoGatewayError,
    SdoTransportError,
)

U = "https://e.com"
INFO = "https://api.scrape.do/info"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"session_id": "abc"}, "session_id must be an integer"),
        ({"session_id": 1.5}, "session_id must be an integer"),
        ({"session_id": True}, "session_id must be an integer"),
        ({"regional_geo_code": "europe"}, "requires super_proxy"),
        (
            {"super_proxy": True, "geo_code": "us", "regional_geo_code": "europe"},
            "cannot be combined",
        ),
        ({"geo_code": "us", "zip_code": "90210"}, "requires super_proxy=True and a geo_code"),
        ({"custom_headers": True, "forward_headers": True}, "cannot be combined"),
        ({"render": True, "retry_timeout": 5000}, "'retry_timeout' cannot be combined"),
        ({"render": True, "screenshot": True}, "'screenshot' requires return_json"),
        ({"render": True, "show_frames": True}, "'show_frames' requires return_json"),
        (
            {"render": True, "return_json": True, "screenshot": True, "full_screenshot": True},
            "cannot be combined",
        ),
        (
            {
                "render": True,
                "return_json": True,
                "particular_screenshot": "h1",
                "play_with_browser": [{"Action": "Click"}],
            },
            "cannot be combined with play_with_browser",
        ),
        ({"width": 1920}, "'width' requires render"),
    ],
)
def test_dependency_rules(options, message):
    with pytest.raises(SdoConfigError, match=message):
        ScrapeConfig(url=U, **options).to_params()


def test_return_json_alone_screenshot_and_session_ids_accepted():
    assert ScrapeConfig(url=U, return_json=True).to_params() == [("url", U), ("returnJSON", "true")]
    assert ScrapeConfig(
        url=U, render=True, return_json=True, particular_screenshot="#price"
    ).to_params() == [
        ("url", U),
        ("render", "true"),
        ("particularScreenShot", "#price"),
        ("returnJSON", "true"),
    ]
    assert ScrapeConfig(url=U, session_id=0).to_params() == [("url", U), ("sessionId", "0")]
    assert ScrapeConfig(url=U, session_id=1_000_000).to_params()[-1] == ("sessionId", "1000000")
    assert ScrapeConfig(url=U, session_id="").to_params() == [("url", U)]


def test_target_url_error_masks_credentials():
    with pytest.raises(SdoConfigError) as info:
        ScrapeConfig(url="https://user:hunter2@e.com:99999/?token=abcdef123")
    message = str(info.value)
    assert "https://***@e.com" in message
    for secret in ("hunter2", "user:", "abcdef123"):
        assert secret not in message


@respx.mock
def test_510_is_not_retried_and_labelled_a_cancellation():
    route = respx.get("https://api.scrape.do/").mock(return_value=httpx.Response(510))
    with Client(token="t", backoff_factor=0) as c, pytest.raises(SdoGatewayError) as info:
        c.scrape(U)
    assert info.value.is_retryable is False
    assert str(info.value).startswith("The request was canceled by the HTTP client (HTTP 510).")
    assert route.call_count == 1


@respx.mock
def test_target_redirected_location():
    respx.get("https://api.scrape.do/").mock(
        return_value=httpx.Response(
            302, headers={"Scrape.do-Target-Redirected-Location": "https://e.com/next"}
        )
    )
    with Client(token="t") as c:
        result = c.scrape(U, disable_redirection=True)
    assert result.status_code == 302
    assert result.target_redirected_location == "https://e.com/next"


@respx.mock
def test_plugin_result_keeps_status_and_cost():
    route = respx.get("https://api.scrape.do/plugin/google/search").mock(
        return_value=httpx.Response(
            200,
            json={"organic": [1]},
            headers={"Content-Type": "application/json", "Scrape.do-Request-Cost": "10"},
        )
    )
    with Client(token="t") as c:
        result = c.plugin_result("google/search", {"q": "x"}, gl="us")
    assert (result.status_code, result.cost, result.url) == (200, 10, "")
    assert result.json() == {"organic": [1]}
    assert route.calls.last.request.url.params["gl"] == "us"


@pytest.mark.parametrize("route_name", ["amazon", "amazon/"])
@respx.mock
def test_amazon_raw_html_route(route_name):
    route = respx.get("https://api.scrape.do/plugin/amazon/").mock(
        return_value=httpx.Response(
            200, text="<html></html>", headers={"Content-Type": "text/html"}
        )
    )
    with Client(token="t") as c:
        out = c.plugin(route_name, {"url": "https://www.amazon.com/dp/B0", "geocode": "us"})
    assert out == b"<html></html>"
    assert route.calls.last.request.url.raw_path.startswith(b"/plugin/amazon/?token=t&geocode=us")


USAGE_BODY = {
    "IsActive": True,
    "ConcurrentRequest": 40,
    "MaxMonthlyRequest": 3500000,
    "RemainingConcurrentRequest": 15,
    "RemainingMonthlyRequest": 2565023,
}


@respx.mock
def test_usage_reads_info_with_the_token_as_query():
    route = respx.get(INFO).mock(return_value=httpx.Response(200, json=USAGE_BODY))
    with Client(token="TKN") as c:
        usage = c.usage()
    assert usage == Usage(
        is_active=True,
        concurrent_request=40,
        max_monthly_request=3500000,
        remaining_concurrent_request=15,
        remaining_monthly_request=2565023,
    )
    assert route.calls.last.request.url.raw_path == b"/info?token=TKN"


@pytest.mark.parametrize(
    ("status", "body", "cls"),
    [
        (
            429,
            {"message": "You have exceeded your request limit. You can send 10 requests per min."},
            SdoConcurrencyError,
        ),
        (502, {"message": "authentication refused"}, SdoGatewayError),
    ],
)
@respx.mock
def test_usage_error_statuses_are_not_retried(status, body, cls):
    route = respx.get(INFO).mock(return_value=httpx.Response(status, json=body))
    with Client(token="t", retries=3, backoff_factor=0) as c, pytest.raises(cls) as info:
        c.usage()
    assert str(info.value).startswith(body["message"])
    assert route.call_count == 1


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b'{"IsActive":true}',
        b"not json",
        b'{"IsActive":"yes","ConcurrentRequest":1,"MaxMonthlyRequest":1,'
        b'"RemainingConcurrentRequest":1,"RemainingMonthlyRequest":1}',
        b'{"IsActive":true,"ConcurrentRequest":1.5,"MaxMonthlyRequest":1,'
        b'"RemainingConcurrentRequest":1,"RemainingMonthlyRequest":1}',
    ],
)
@respx.mock
def test_usage_rejects_unexpected_bodies(body):
    respx.get(INFO).mock(return_value=httpx.Response(200, content=body))
    with Client(token="t") as c, pytest.raises(SdoTransportError):
        c.usage()


@respx.mock
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("render", [True, "true"])
def test_render_requires_get(method, render):
    route = respx.route(host="api.scrape.do").mock(return_value=httpx.Response(200))
    with Client(token="t") as c, pytest.raises(SdoConfigError) as info:
        c.scrape(U, render=render, method=method)
    assert str(info.value) == f"render works with GET requests only; got {method}."
    assert route.call_count == 0


@respx.mock
def test_rendered_get_and_unrendered_post_are_sent():
    route = respx.route(host="api.scrape.do").mock(return_value=httpx.Response(200))
    with Client(token="t") as c:
        c.scrape(U, render=True)
        c.scrape(U, render=False, method="POST", data="a=1")
    assert route.call_count == 2
