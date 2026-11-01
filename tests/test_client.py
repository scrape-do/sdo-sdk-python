# SPDX-License-Identifier: Apache-2.0
import httpx
import pytest
import respx

from sdo import Client, ScrapeConfig
from sdo.errors import SdoConfigError

API = "https://api.scrape.do/"


@respx.mock
def test_scrape_builds_request_and_result():
    route = respx.get(API).mock(
        return_value=httpx.Response(
            200,
            text="<html>ok</html>",
            headers={"Scrape.do-Request-Cost": "5"},
        )
    )
    with Client(token="TKN") as c:
        result = c.scrape("https://example.com", render=True, geo_code="us")

    assert result.status_code == 200
    assert "ok" in result.text
    assert (result.cost, result.url) == (5, "https://example.com")

    req = route.calls.last.request
    assert req.headers["accept-encoding"] == "gzip, deflate, br"
    assert req.headers["user-agent"].startswith("sdo-sdk-python/")
    assert req.url.raw_path == (b"/?token=TKN&url=https%3A%2F%2Fexample.com&render=true&geoCode=us")


@respx.mock
def test_scrape_accepts_scrapeconfig_and_url_keyword():
    route = respx.get(API).mock(return_value=httpx.Response(200, text="x"))
    with Client(token="t") as c:
        c.scrape(ScrapeConfig(url="https://e.com", super_proxy=True))
        c.scrape(url="https://e.com")
    assert route.calls[0].request.url.params["super"] == "true"
    assert route.call_count == 2


def test_config_or_kwargs_not_both_and_unknown_options():
    with Client(token="t") as c:
        with pytest.raises(SdoConfigError):
            c.scrape(ScrapeConfig(url="https://e.com"), render=True)
        with pytest.raises(SdoConfigError, match="Invalid scrape"):
            c.scrape("https://e.com", no_such_option=True)
        with pytest.raises(SdoConfigError):
            c.scrape()


def test_token_explicit_param_wins(monkeypatch):
    monkeypatch.setenv("SDO_TOKEN", "envtok")
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, text="x"))
        with Client(token="paramtok") as c:
            c.scrape("https://e.com")
        assert route.calls.last.request.url.params["token"] == "paramtok"


def test_token_env_used_when_no_param(monkeypatch):
    monkeypatch.setenv("SDO_TOKEN", "envtok")
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, text="x"))
        with Client() as c:
            c.scrape("https://e.com")
        assert route.calls.last.request.url.params["token"] == "envtok"


def test_no_token_raises():
    with pytest.raises(SdoConfigError):
        Client()


@pytest.mark.parametrize("token", ["has space", "in\tner", "é-token", "del\x7f", 12345])
def test_token_must_be_visible_ascii(token):
    with pytest.raises(SdoConfigError, match="token"):
        Client(token=token)  # type: ignore[arg-type]


def test_invalid_sdo_token_is_named_in_the_error(monkeypatch):
    monkeypatch.setenv("SDO_TOKEN", "has space")
    with pytest.raises(SdoConfigError, match="SDO_TOKEN"):
        Client()


def test_token_whitespace_is_trimmed(monkeypatch):
    """A token read from a secret file usually ends in a newline."""
    monkeypatch.setenv("SDO_TOKEN", "  envtok\n")
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200))
        with Client() as c:
            c.scrape("https://e.com")
        assert route.calls.last.request.url.params["token"] == "envtok"


@respx.mock
def test_prefer_brotli_sets_br_only():
    route = respx.get(API).mock(return_value=httpx.Response(200, text="x"))
    with Client(token="t", prefer_brotli=True) as c:
        c.scrape("https://e.com")
    assert route.calls.last.request.headers["accept-encoding"] == "br"


@respx.mock
def test_base_urls_from_environment(monkeypatch):
    monkeypatch.setenv("SDO_BASE_URL", "https://proxy.internal/gw/")
    monkeypatch.setenv("SDO_ASYNC_BASE_URL", "https://jobs.internal")
    sync = respx.get("https://proxy.internal/gw/").mock(return_value=httpx.Response(200))
    jobs = respx.get("https://jobs.internal/api/v1/me").mock(
        return_value=httpx.Response(200, json={})
    )
    with Client(token="t") as c:
        c.scrape("https://e.com")
        c.jobs.me()
    assert sync.called and jobs.called


@pytest.mark.parametrize(
    "url",
    [
        "https://h/?q=1",
        "https://h/#f",
        "https://h:99999",
        "https://",
        42,
        "https://[::1",
        "http\u017f://h",  # long s: case-folds to "s" without re.ASCII
    ],
)
def test_malformed_base_urls_are_config_errors(url):
    with pytest.raises(SdoConfigError, match="base_url"):
        Client(token="t", base_url=url)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timeout": 0}, "timeout"),
        ({"timeout": float("inf")}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"timeout": "30"}, "timeout"),
        ({"retries": 1.5}, "retries"),
        ({"retries": True}, "retries"),
        ({"backoff_factor": -1}, "backoff_factor"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
    ],
)
def test_invalid_client_settings_are_config_errors(kwargs, match):
    with pytest.raises(SdoConfigError, match=match):
        Client(token="t", **kwargs)


def test_explicit_timeout_wins_over_environment(monkeypatch):
    monkeypatch.setenv("SDO_TIMEOUT", "not-even-a-number")
    c = Client(token="t", timeout=5)
    assert c._timeout == 5
    c.close()


@pytest.mark.parametrize("value", ["2", "2.5", "0.001", "007.50"])
def test_sdo_timeout_accepts_decimal_forms(value, monkeypatch):
    monkeypatch.setenv("SDO_TIMEOUT", value)
    with Client(token="t") as c:
        assert c._timeout == float(value)


@pytest.mark.parametrize(
    "value", ["١٢", "1_0", "0x10", "-0", "1e3", "+2.5", ".5", " 30 ", "0.0009"]
)
def test_sdo_timeout_rejects_other_forms(value, monkeypatch):
    monkeypatch.setenv("SDO_TIMEOUT", value)
    with pytest.raises(SdoConfigError, match="SDO_TIMEOUT"):
        Client(token="t")


@respx.mock
def test_post_with_json_body_is_canonical():
    route = respx.post(API).mock(return_value=httpx.Response(200, text="x"))
    with Client(token="t") as c:
        c.scrape("https://e.com", json={"b": 1.0, "a": "<"})
    req = route.calls.last.request
    assert req.method == "POST"
    assert req.content == b'{"a":"<","b":1}'


@respx.mock
def test_data_body_promotes_get_to_post_and_bytes_are_sent_as_is():
    route = respx.post(API).mock(return_value=httpx.Response(200, text="x"))
    with Client(token="t") as c:
        c.scrape("https://e.com", data="raw body")
        c.scrape("https://e.com", data=bytearray(b"\x00\xff"))
    assert route.calls[0].request.content == b"raw body"
    assert route.calls[1].request.content == b"\x00\xff"


@respx.mock
def test_content_type_from_config_headers_is_respected():
    route = respx.put(API).mock(return_value=httpx.Response(200))
    with Client(token="t", retries=0) as c:
        c.scrape(
            ScrapeConfig(url="https://e.com", headers={"content-type": "text/json"}),
            method="put",
            json={"a": 1},
        )
    assert route.calls.last.request.headers.get_list("content-type") == ["text/json"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"data": {"form": "dict"}},
        {"json": {"k": object()}},
        {"method": 5},
        {"url": "https://e.com/\ud800"},
    ],
)
def test_bad_request_inputs_are_config_errors(kwargs):
    with respx.mock(assert_all_called=False) as mock, Client(token="t") as c:
        if "url" in kwargs:
            with pytest.raises(SdoConfigError):
                c.scrape(kwargs["url"])
        else:
            with pytest.raises(SdoConfigError):
                c.scrape("https://e.com", **kwargs)
        assert mock.calls.call_count == 0


@respx.mock
def test_plugin_call_returns_json():
    route = respx.get("https://api.scrape.do/plugin/google/shopping").mock(
        return_value=httpx.Response(
            200, json={"results": [1, 2]}, headers={"Content-Type": "application/json"}
        )
    )
    with Client(token="t") as c:
        data = c.plugin("google/shopping", {"q": "laptop"}, gl="us")
    assert data == {"results": [1, 2]}
    assert route.calls.last.request.url.raw_path == (
        b"/plugin/google/shopping?token=t&gl=us&q=laptop"
    )


@respx.mock
def test_body_keeps_an_explicit_non_get_method():
    """A body switches GET to POST, but never overrides a method the caller chose."""
    route = respx.put(API).mock(return_value=httpx.Response(200))
    with Client(token="t", retries=0) as c:
        c.scrape("https://e.com", method="PUT", json={"a": 1})
        c.scrape("https://e.com", method="PUT", data="raw")
    assert route.call_count == 2
    assert route.calls[0].request.headers["content-type"] == "application/json"


def test_jobs_client_is_created_once():
    with Client(token="t") as c:
        assert c.jobs is c.jobs


def test_injected_http_client_is_not_closed_by_the_sdk():
    """The caller owns a client it passes in; closing ours must leave theirs usable."""
    mine = httpx.Client()
    c = Client(token="t", http_client=mine)
    _ = c.jobs
    c.close()
    assert not mine.is_closed
    mine.close()


def test_loopback_http_needs_no_opt_in():
    with respx.mock:
        route = respx.get("http://127.0.0.1:8080/").mock(return_value=httpx.Response(200))
        with Client(token="t", base_url="http://127.0.0.1:8080") as c:
            c.scrape("https://e.com")
    assert route.called


@pytest.mark.parametrize(
    "url",
    [
        "https://user:hunter2@api.scrape.do",
        "http://user:hunter2@evil.example",
        "https://user:hunter2@api.scrape.do/ x",
        "https://a@b:hunter2@h/?q",
    ],
)
def test_base_url_errors_mask_credentials(url):
    with pytest.raises(SdoConfigError) as ei:
        Client(token="t", base_url=url)
    assert "hunter2" not in str(ei.value)
    assert "***@" in str(ei.value)
