# SPDX-License-Identifier: Apache-2.0
import httpx
import pytest
import respx

from sdo import Client
from sdo.errors import (
    SdoConfigError,
    SdoConnectionError,
    SdoTransportError,
    SdoValidationError,
)
from sdo.plugins import plugin_timeout_ms


@respx.mock
def test_plugin_returns_raw_bytes_for_non_json():
    route = respx.get("https://api.scrape.do/plugin/amazon/product").mock(
        return_value=httpx.Response(
            200, content=b"\x89PNG-raw", headers={"Content-Type": "image/png"}
        )
    )
    with Client(token="t") as c:
        out = c.plugin("amazon/product", {"flag": False, "ids": [1, 2.0, None], "skip": None})
    assert out == b"\x89PNG-raw"
    assert route.calls.last.request.url.raw_path == (
        b"/plugin/amazon/product?token=t&flag=false&ids=1&ids=2"
    )


@respx.mock
def test_plugin_dict_values_are_canonical_json():
    route = respx.get("https://api.scrape.do/plugin/a/b").mock(return_value=httpx.Response(200))
    with Client(token="t") as c:
        c.plugin("a/b", {"filter": {"z": 1, "a": [True]}})
    assert route.calls.last.request.url.params["filter"] == '{"a":[true],"z":1}'


@pytest.mark.parametrize(
    "params",
    [
        {"x": object()},
        {"x": float("nan")},
        {1: "int key"},
        {"": "empty"},
        {"x": "\ud800"},
        {"x": {"nan": float("nan")}},
        {"x": {1: "int key inside an object"}},
        {"x": [["nested"]]},
        {"x": [("nested",)]},
        {"tOkEn": "reserved in any case"},
        {"a b": "space"},
        {"q&token=x": "query injection"},
        {"k[]": "brackets"},
        {"1q": "leading digit"},
        {"tok\u0435n": "Cyrillic look-alike"},
    ],
)
def test_plugin_rejects_unsupported_params(params):
    with respx.mock(assert_all_called=False) as mock, Client(token="t") as c:
        with pytest.raises(SdoConfigError):
            c.plugin("a/b", params)
        assert mock.calls.call_count == 0


def test_plugin_param_names_must_be_parameter_names():
    with respx.mock(assert_all_called=False) as mock, Client(token="t") as c:
        with pytest.raises(SdoConfigError, match="'a b' is not valid"):
            c.plugin("a/b", {"a b": "x"})
        with pytest.raises(SdoConfigError, match="may not set 'TOKEN'"):
            c.plugin("a/b", {"TOKEN": "x"})  # the reserved-key message wins
        assert mock.calls.call_count == 0


@respx.mock
def test_plugin_accepts_the_documented_name_shapes():
    route = respx.get("https://api.scrape.do/plugin/a/b").mock(
        return_value=httpx.Response(200, json={})
    )
    with Client(token="t") as c:
        c.plugin("a/b", {"q": "x", "hl": "en", "include_airlines": "TK", "geo.code": 1, "x-y": 2})
    assert "include_airlines=TK" in str(route.calls.last.request.url)


@respx.mock
def test_plugin_invalid_json_is_a_transport_error():
    respx.get("https://api.scrape.do/plugin/a/b").mock(
        return_value=httpx.Response(200, text="{nope", headers={"Content-Type": "application/json"})
    )
    with Client(token="t") as c, pytest.raises(SdoTransportError, match="not valid JSON"):
        c.plugin("a/b")


@respx.mock
def test_connection_error_maps_to_sdo_connection_error():
    respx.get("https://api.scrape.do/").mock(side_effect=httpx.ConnectError("boom"))
    with Client(token="t", retries=0, backoff_factor=0.0) as c:
        with pytest.raises(SdoConnectionError):
            c.scrape("https://e.com")


@pytest.mark.parametrize("method", ["plugin", "plugin_result"])
@pytest.mark.parametrize("params", [[("a", "b")], "abc", 5])
def test_plugin_params_must_be_a_mapping(method, params):
    with respx.mock(assert_all_called=False) as mock, Client(token="t") as c:
        with pytest.raises(SdoConfigError, match="mapping"):
            getattr(c, method)("a/b", params)
        assert mock.calls.call_count == 0


# A plugin call follows the scrape status rules: a target 404/410, and a 400 the
# gateway billed, are results; the gateway's own 400 is an unbilled validation
# error whose message is the plugin envelope's, read in any letter case.
@pytest.mark.parametrize(
    ("status", "headers"),
    [(404, {}), (410, {}), (400, {"Scrape.do-Request-Cost": "25"})],
    ids=["target_404", "target_410", "billed_target_400"],
)
@respx.mock
def test_plugin_target_statuses_are_results(status, headers):
    respx.get("https://api.scrape.do/plugin/google/search").mock(
        return_value=httpx.Response(status, content=b"page", headers=headers)
    )
    with Client(token="t", retries=0) as c:
        assert c.plugin("google/search", {"q": "x"}) == b"page"


@pytest.mark.parametrize(
    ("body", "headers", "message"),
    [
        (
            '{"error":"invalid_zipcode",'
            '"message":"ZIP code format doesn\'t match country requirements"}',
            {"Scrape.do-Plugin": "google/search"},
            "ZIP code format doesn't match country requirements",
        ),
        ('{"error":"timeout is invalid"}', {}, "timeout is invalid"),
        ('{"Message":"nope"}', {"Scrape.do-Request-Cost": "0"}, "nope"),
        # A message that is neither a string nor a list falls through to error.
        ('{"message":{"code":1},"error":"bad_zip"}', {}, "bad_zip"),
    ],
    ids=["error_and_message", "error_only", "zero_cost", "non_text_message"],
)
@respx.mock
def test_plugin_gateway_400_is_an_unbilled_validation_error(body, headers, message):
    respx.get("https://api.scrape.do/plugin/google/search").mock(
        return_value=httpx.Response(400, text=body, headers=headers)
    )
    with Client(token="t", retries=0) as c, pytest.raises(SdoValidationError) as ei:
        c.plugin("google/search", {"q": "x"})
    assert ei.value.message == message
    assert ei.value.billed is False


@respx.mock
def test_plugin_timeout_param_gets_the_gateway_margin():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    respx.get("https://api.scrape.do/plugin/walmart/search").mock(side_effect=handler)
    with Client(token="t", timeout=5) as c:
        c.plugin("walmart/search", {"q": "x", "timeout": 30000})
        c.plugin("walmart/search", {"q": "x"})
        c.plugin("walmart/search", {"q": "x", "timeout": "abc"})
    assert [r.extensions["timeout"]["read"] for r in seen] == [40.0, 5, 5]


@pytest.mark.parametrize(
    ("pairs", "want"),
    [
        ([], None),
        ([("q", "x")], None),
        ([("timeout", "30000")], 30000),
        ([("timeout", "0")], None),
        ([("timeout", "1.5")], None),
        ([("timeout", "-5")], None),
        ([("timeout", "١٢")], None),
        ([("timeout", "1000"), ("timeout", "2000")], None),
        ([("Timeout", "1000")], None),
    ],
)
def test_plugin_timeout_ms(pairs, want):
    assert plugin_timeout_ms(pairs) == want
