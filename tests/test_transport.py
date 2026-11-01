# SPDX-License-Identifier: Apache-2.0
import gzip
import time

import httpx
import pytest
import respx

from sdo import Client, ScrapeConfig, _transport
from sdo.errors import (
    SdoAPIError,
    SdoAuthError,
    SdoConcurrencyError,
    SdoConfigError,
    SdoConnectionError,
    SdoGatewayError,
    SdoTimeoutError,
    SdoTransportError,
    SdoValidationError,
)
from sdo.response import ScrapeResult

API = "https://api.scrape.do/"
U = "https://e.com"


def client(**kw):
    kw.setdefault("backoff_factor", 0.0)  # keep retry tests instant
    return Client(token="t", **kw)


class Raw(httpx.SyncByteStream):
    """A body httpx has not read yet (``content=`` would decode it eagerly)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self):
        yield self.data


class Drip(httpx.SyncByteStream):
    """A body that arrives one small chunk at a time."""

    def __init__(self, chunks: int, delay: float) -> None:
        self.chunks, self.delay = chunks, delay

    def __iter__(self):
        for _ in range(self.chunks):
            time.sleep(self.delay)
            yield b"x"


@respx.mock
def test_transparent_gzip_decompression():
    body = b"<html>hello compressed</html>"
    respx.get(API).mock(
        return_value=httpx.Response(
            200,
            content=gzip.compress(body),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/html"},
        )
    )
    with client() as c:
        r = c.scrape(U)
    assert r.bytes() == body
    assert "hello compressed" in r.text


@respx.mock
def test_transparent_brotli_decompression():
    import brotli

    body = b"<html>brotli body</html>"
    respx.get(API).mock(
        return_value=httpx.Response(
            200, content=brotli.compress(body), headers={"Content-Encoding": "br"}
        )
    )
    with client(prefer_brotli=True) as c:
        assert c.scrape(U).bytes() == body


@respx.mock
def test_unsupported_content_encoding_is_returned_as_received():
    respx.get(API).mock(
        return_value=httpx.Response(
            200, content=b"\x28\xb5\x2f\xfd", headers={"Content-Encoding": "zstd"}
        )
    )
    with client() as c:
        assert c.scrape(U).bytes() == b"\x28\xb5\x2f\xfd"


@respx.mock
def test_corrupt_compressed_body_is_a_transport_error():
    """httpx.DecodingError used to escape the taxonomy (with the token URL on
    its .request)."""
    respx.get(API).mock(
        return_value=httpx.Response(
            200, stream=Raw(b"not gzip"), headers={"Content-Encoding": "gzip"}
        )
    )
    with client(retries=2) as c, pytest.raises(SdoTransportError, match="decompressed") as ei:
        c.scrape(U)
    assert type(ei.value) is SdoTransportError
    assert respx.calls.call_count == 1  # deterministic: not retried


@respx.mock
def test_raw_body_over_the_cap_is_refused():
    respx.get(API).mock(return_value=httpx.Response(200, content=b"x" * 2048))
    with client(max_response_bytes=1024) as c, pytest.raises(_transport.SdoResponseTooLargeError):
        c.scrape(U)


@respx.mock
def test_401_raises_auth_error():
    respx.get(API).mock(return_value=httpx.Response(401, text="bad token"))
    with client() as c, pytest.raises(SdoAuthError) as ei:
        c.scrape(U)
    assert ei.value.status_code == 401
    assert "check your token" in ei.value.message
    assert ei.value.body == "bad token"


@respx.mock
def test_exhausted_credit_is_an_auth_error_not_a_quota_tier():
    """There is no 402 tier: a depleted balance also returns 401, so it
    surfaces as SdoAuthError and is told apart by the message."""
    respx.get(API).mock(
        return_value=httpx.Response(401, json={"Message": ["Monthly credit limit reached"]})
    )
    with client() as c, pytest.raises(SdoAuthError) as ei:
        c.scrape(U)
    assert "credit" in str(ei.value).lower()
    assert ei.value.billed is False


@respx.mock
def test_unbilled_400_raises_validation_error_not_billed():
    respx.get(API).mock(return_value=httpx.Response(400, text="bad param"))
    with client() as c, pytest.raises(SdoValidationError) as ei:
        c.scrape(U)
    assert ei.value.billed is False


#: Exactly what the gateway writes for render=mert: it answers a parameter it
#: cannot parse before authorizing the request, so no Scrape.do-Request-Cost.
_GATEWAY_RENDER_400 = (
    '{"URL":"https://e.com","StatusCode":400,"Message":["\'Render\' parameter should boolean."],'
    '"PossibleCauses":["Please check your request."],'
    '"Contact":"Please don\'t hesitate to contact with us via support@scrape.do."}'
)


@respx.mock
def test_gateway_400_bad_parameter_is_an_unbilled_validation_error():
    route = respx.get(API).mock(
        return_value=httpx.Response(
            400,
            text=_GATEWAY_RENDER_400,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Content-Type-Options": "nosniff",
            },
        )
    )
    with client() as c, pytest.raises(SdoValidationError) as ei:
        c.scrape(ScrapeConfig(U, extra_params={"render": "mert"}))
    assert route.calls.last.request.url.params["render"] == "mert"
    assert route.call_count == 1  # never retried
    assert ei.value.message == "'Render' parameter should boolean."
    assert ei.value.billed is False
    assert ei.value.is_retryable is False


@respx.mock
def test_target_400_the_gateway_billed_is_a_result():
    respx.get(API).mock(
        return_value=httpx.Response(
            400,
            text="<html>Bad Request</html>",
            headers={"Scrape.do-Request-Cost": "1", "Scrape.do-Initial-Status-Code": "400"},
        )
    )
    with client() as c:
        r = c.scrape(U)
    assert r.status_code == 400
    assert r.text == "<html>Bad Request</html>"
    assert r.cost == 1


@pytest.mark.parametrize("status", [404, 410])
@respx.mock
def test_target_404_and_410_are_results(status):
    respx.get(API).mock(return_value=httpx.Response(status, text="gone"))
    with client() as c:
        r = c.scrape(U)
    assert r.status_code == status
    assert r.text == "gone"


@respx.mock
def test_error_carries_concurrency_details():
    respx.get(API).mock(
        return_value=httpx.Response(
            429,
            headers={
                "Scrape.do-Remaining-Credits": "١٢".encode(),
                "Retry-After": "7",
            },
        )
    )
    with client(retries=0) as c, pytest.raises(SdoConcurrencyError) as ei:
        c.scrape(U)
    err = ei.value
    assert err.retry_after == 7
    assert err.remaining_credits is None  # non-ASCII digits never parse


@respx.mock
def test_target_404_is_a_result_not_an_error():
    respx.get(API).mock(return_value=httpx.Response(404, text="not found"))
    with client() as c:
        r = c.scrape(U)
    assert r.status_code == 404
    assert "not found" in r.text


@respx.mock
def test_a_redirect_is_returned_not_followed():
    respx.get(API).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.test/"})
    )
    with client() as c:
        r = c.scrape(U)
    assert r.status_code == 302
    assert respx.calls.call_count == 1


@respx.mock
def test_429_is_retried_then_succeeds():
    route = respx.get(API)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, text="ok"),
    ]
    with client() as c:
        assert c.scrape(U).status_code == 200
    assert route.call_count == 2


@respx.mock
def test_retry_after_is_honoured_and_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(_transport.time, "sleep", slept.append)
    respx.get(API).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "3"}),
            httpx.Response(503, headers={"Retry-After": "3600"}),
            httpx.Response(200),
        ]
    )
    with client() as c:
        c.scrape(U)
    assert slept == [3, 30.0]


@respx.mock
def test_backoff_is_exponential_with_proportional_jitter(monkeypatch):
    slept = []
    monkeypatch.setattr(_transport.time, "sleep", slept.append)
    monkeypatch.setattr(_transport.random, "uniform", lambda lo, hi: hi)
    respx.get(API).mock(side_effect=[httpx.Response(502), httpx.Response(502), httpx.Response(200)])
    with client(backoff_factor=1.0) as c:
        c.scrape(U)
    assert slept == [pytest.approx(1.1), pytest.approx(2.2)]


@respx.mock
def test_malformed_retry_after_falls_back_to_backoff(monkeypatch):
    """'-1' and 'nan' used to reach time.sleep and raise a bare ValueError."""
    slept = []
    monkeypatch.setattr(_transport.time, "sleep", slept.append)
    respx.get(API).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "-1"}),
            httpx.Response(429, headers={"Retry-After": "nan"}),
            httpx.Response(200),
        ]
    )
    with client() as c:
        assert c.scrape(U).status_code == 200
    assert slept == [0.0, 0.0]


@respx.mock
def test_502_exhausted_raises_gateway_error():
    respx.get(API).mock(return_value=httpx.Response(502))
    with client(retries=1) as c, pytest.raises(SdoGatewayError) as ei:
        c.scrape(U)
    assert ei.value.is_retryable is True
    assert respx.calls.call_count == 2


@respx.mock
def test_timeout_raises_after_retries():
    route = respx.get(API).mock(side_effect=httpx.ConnectTimeout("slow"))
    with client(retries=1) as c, pytest.raises(SdoTimeoutError):
        c.scrape(U)
    assert route.call_count == 2


def test_timeout_is_a_total_deadline_over_the_body():
    """httpx's timeout is per read, so a body dripping in under it used to run
    far past the client's timeout."""
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, stream=Drip(40, 0.02)))
        started = time.monotonic()
        with client(timeout=0.2, retries=0) as c, pytest.raises(SdoTimeoutError, match="total"):
            c.scrape(U)
    assert time.monotonic() - started < 0.6


def test_the_timeout_is_passed_to_every_request():
    seen = []
    with respx.mock:
        respx.get(API).mock(side_effect=lambda r: seen.append(r) or httpx.Response(200))
        with client(timeout=7) as c:
            c.scrape(U)
    assert seen[0].extensions["timeout"] == {"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}


@pytest.mark.parametrize(
    ("exc", "get_attempts"),
    [
        (httpx.ConnectError("refused"), 3),  # never reached the server: safe to resend
        (httpx.ConnectTimeout("slow"), 3),
        (httpx.PoolTimeout("busy"), 3),
        (httpx.ReadTimeout("slow"), 1),  # sent: the gateway may still run and bill it
        (httpx.WriteTimeout("slow"), 1),
        (httpx.ReadError("reset"), 1),
        (httpx.RemoteProtocolError("cut"), 1),
    ],
)
def test_only_failures_before_sending_are_retried_and_only_when_idempotent(exc, get_attempts):
    with respx.mock:
        get = respx.get(API).mock(side_effect=exc)
        post = respx.post(API).mock(side_effect=exc)
        with client(retries=2) as c:
            with pytest.raises((SdoConnectionError, SdoTimeoutError)):
                c.scrape(U)
            with pytest.raises((SdoConnectionError, SdoTimeoutError)):
                c.scrape(U, data="x")
    assert (get.call_count, post.call_count) == (get_attempts, 1)


class Stall(httpx.SyncByteStream):
    """Some body, then a read that fails mid-response."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __iter__(self):
        yield b"partial"
        raise self.exc


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ReadError("reset")])
def test_a_failure_mid_body_is_not_retried(exc):
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, stream=Stall(exc)))
        with client(retries=2) as c, pytest.raises((SdoConnectionError, SdoTimeoutError)):
            c.scrape(U)
    assert route.call_count == 1


def test_an_oversized_body_is_not_retried():
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, stream=Raw(b"x" * 2048)))
        with client(retries=2, max_response_bytes=1024) as c:
            with pytest.raises(_transport.SdoResponseTooLargeError):
                c.scrape(U)
    assert route.call_count == 1


def test_the_timeout_error_names_the_effective_timeout():
    with respx.mock:
        respx.get(API).mock(side_effect=httpx.ReadTimeout("timed out"))
        with client(timeout=7, retries=0) as c:
            with pytest.raises(SdoTimeoutError, match="client timeout 7s"):
                c.scrape(U)
            with pytest.raises(SdoTimeoutError, match="client timeout 40s"):
                c.scrape(U, timeout=30000)


def test_an_error_body_that_fails_mid_read_still_raises_the_status_error():
    stream = Stall(httpx.ReadTimeout("slow"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(502, stream=stream))
        with client(retries=0) as c, pytest.raises(SdoGatewayError) as ei:
            c.scrape(U)
    assert ei.value.body == "partial"
    assert ei.value.message == "Gateway-origin upstream failure (HTTP 502)."


def test_a_corrupt_compressed_error_body_still_raises_the_status_error():
    with respx.mock:
        respx.get(API).mock(
            return_value=httpx.Response(
                400,
                stream=Raw(gzip.compress(b'{"Message":"bad geo"}')[:-8] + b"\xff" * 8),
                headers={"Content-Encoding": "gzip"},
            )
        )
        with client(retries=0) as c, pytest.raises(SdoValidationError) as ei:
            c.scrape(U)
    assert ei.value.billed is False
    assert ei.value.message == "The gateway rejected the request parameters."


def test_an_error_body_that_breaks_off_is_not_trusted_for_its_envelope():
    """The prefix read before the failure is a whole envelope, but a body that
    did not arrive intact yields the status's default message."""
    stream = Stall(httpx.ReadError("reset"))
    stream_body = b'{"Message":"early"}'
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(400, stream=_Prefixed(stream_body, stream)))
        with client(retries=0) as c, pytest.raises(SdoValidationError) as ei:
            c.scrape(U)
    assert ei.value.body == '{"Message":"early"}partial'
    assert ei.value.message == "The gateway rejected the request parameters."


class _Prefixed(httpx.SyncByteStream):
    def __init__(self, first: bytes, rest: httpx.SyncByteStream) -> None:
        self.first, self.rest = first, rest

    def __iter__(self):
        yield self.first
        yield from self.rest


def test_an_error_body_over_the_raw_cap_keeps_what_decoded():
    """Incompressible gzip is larger than its content: the raw cap can end
    the read while the decoded prefix is still under the 64 KiB limit."""
    import os

    content = os.urandom(1010)
    with respx.mock:
        respx.get(API).mock(
            return_value=httpx.Response(
                502, stream=Raw(gzip.compress(content)), headers={"Content-Encoding": "gzip"}
            )
        )
        with client(retries=0, max_response_bytes=1024) as c, pytest.raises(SdoGatewayError):
            c.scrape(U)


def test_an_oversized_error_body_envelope_is_parsed_from_its_first_64_kib():
    body = b'{"Message":"early"}' + b" " * (100 * 1024)
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(502, stream=Raw(body)))
        with client(retries=0) as c, pytest.raises(SdoGatewayError) as ei:
            c.scrape(U)
    assert ei.value.message == "early"
    compressed = gzip.compress(body)
    with respx.mock:
        respx.get(API).mock(
            return_value=httpx.Response(
                502, stream=Raw(compressed), headers={"Content-Encoding": "gzip"}
            )
        )
        with client(retries=0) as c, pytest.raises(SdoGatewayError) as ei:
            c.scrape(U)
    assert ei.value.message == "early"


@pytest.mark.parametrize(
    "exc",
    [httpx.DecodingError("bad"), httpx.InvalidURL("bad"), httpx.StreamConsumed()],
)
def test_other_httpx_errors_stay_inside_the_taxonomy(exc):
    with respx.mock:
        respx.get(API).mock(side_effect=exc)
        with client(retries=2) as c, pytest.raises(SdoTransportError) as ei:
            c.scrape(U)
    assert type(ei.value) is SdoTransportError
    assert type(exc).__name__ in ei.value.message


def test_errors_do_not_chain_the_httpx_exception():
    """The httpx exception's text and .request carry the token URL; the raised
    error has neither a cause nor a context pointing at it."""
    with respx.mock:
        respx.get(API).mock(side_effect=httpx.ConnectError("refused"))
        with client(retries=0) as c, pytest.raises(SdoConnectionError) as ei:
            c.scrape(U)
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None


def test_unmapped_4xx_becomes_the_base_api_error():
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(418))
        with client(retries=0) as c, pytest.raises(SdoAPIError) as ei:
            c.scrape(U)
    assert ei.value.status_code == 418
    assert type(ei.value) is SdoAPIError


def test_config_headers_and_call_headers_both_reach_the_request():
    seen = []
    with respx.mock:
        respx.get(API).mock(side_effect=lambda r: seen.append(r) or httpx.Response(200))
        with client() as c:
            c.scrape(
                ScrapeConfig(url=U, headers={"X-From-Config": "1", "X-Both": "config"}),
                headers={"X-From-Call": "2", "X-Both": "call"},
            )
    headers = seen[0].headers
    assert (headers["x-from-config"], headers["x-from-call"], headers["x-both"]) == (
        "1",
        "2",
        "call",
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Bad": "a\r\nInjected: 1"},
        {"Bad Name": "v"},
        {"X-Uni": "é"},
        {"X-Num": 5},
        {"X-Del": "a\x7fb"},
        {"Transfer-Encoding": "chunked"},
        {"CONTENT-LENGTH": "5"},
        [("X-A", "b")],
    ],
)
def test_unsafe_headers_are_rejected_before_sending(headers):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(API).mock(return_value=httpx.Response(200))
        with client(retries=2) as c, pytest.raises(SdoConfigError, match="header"):
            c.scrape(U, headers=headers)
        with pytest.raises(SdoConfigError, match="header"):
            c.scrape(ScrapeConfig(url=U, headers=headers))  # type: ignore[arg-type]
    assert mock.calls.call_count == 0


def test_header_names_merge_case_insensitively_and_the_last_value_wins():
    seen = []
    with respx.mock:
        respx.get(API).mock(side_effect=lambda r: seen.append(r) or httpx.Response(200))
        with client() as c:
            c.scrape(
                ScrapeConfig(url=U, headers={"X-Both": "config", "user-agent": "from-config"}),
                headers={"x-both": "call", "User-Agent": "Mozilla/5.0"},
            )
    raw = [(k.decode().lower(), v.decode()) for k, v in seen[0].headers.raw]
    assert [v for k, v in raw if k == "x-both"] == ["call"]
    assert [v for k, v in raw if k == "user-agent"] == ["Mozilla/5.0"]
    assert sorted(k for k, _ in raw) == ["accept-encoding", "host", "user-agent", "x-both"]


def test_an_injected_clients_default_headers_are_not_sent():
    seen = []
    injected = httpx.Client(
        headers={"X-Default": "1"},
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
    )
    with Client(token="t", http_client=injected) as c:
        c.scrape(U, data="body")
    injected.close()
    names = sorted(k.decode().lower() for k, _ in seen[0].headers.raw)
    assert names == ["accept-encoding", "content-length", "host", "user-agent"]


def test_an_invalid_accept_encoding_is_a_config_error():
    with pytest.raises(SdoConfigError, match="header"):
        Client(token="t", accept_encoding="gzip\r\nX-Injected: 1")


def test_a_json_body_that_cannot_be_encoded_is_a_config_error():
    cyclic: list = []
    cyclic.append(cyclic)
    with respx.mock(assert_all_called=False) as mock, client() as c:
        for body in (cyclic, {"n": float("nan")}):
            with pytest.raises(SdoConfigError, match="json="):
                c.scrape(U, json=body)
        with pytest.raises(SdoConfigError, match="data="):
            c.scrape(U, data="\ud800")
    assert mock.calls.call_count == 0


def test_an_error_body_that_outlives_the_deadline_still_raises_the_status_error():
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(503, stream=Drip(40, 0.02)))
        started = time.monotonic()
        with client(timeout=0.2, retries=0) as c, pytest.raises(SdoGatewayError) as ei:
            c.scrape(U)
    assert time.monotonic() - started < 0.6
    assert 0 < len(ei.value.body or "") < 40


def test_async_errors_read_only_the_error_key():
    with respx.mock:
        respx.get("https://q.scrape.do/api/v1/me").mock(
            side_effect=[
                httpx.Response(409, json=["Error"]),
                httpx.Response(409, json={"Error": ""}),
                httpx.Response(409, json={"Error": 5, "Message": "sync-style"}),
            ]
        )
        with client(retries=0) as c:
            for _ in range(3):
                with pytest.raises(SdoAPIError) as ei:
                    c.jobs.me()
                assert ei.value.message == "Request failed (HTTP 409)."
                assert ei.value.billed is False


def test_use_after_close_is_a_config_error():
    c = client()
    c.close()
    c.close()  # idempotent
    with pytest.raises(SdoConfigError, match="closed"):
        c.scrape(U)
    with pytest.raises(SdoConfigError, match="closed"):
        c.jobs.me()


def test_injected_client_keeps_the_sdk_urls_redirect_policy_and_timeout():
    """An injected httpx.Client used to replace the base URLs (jobs went to the
    sync host) and its own redirect/timeout settings applied."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.test/"}, json={})

    injected = httpx.Client(
        base_url="https://wrong.test", follow_redirects=True, transport=httpx.MockTransport(handler)
    )
    c = Client(token="t", http_client=injected, timeout=3, retries=0)
    assert c.scrape(U).status_code == 302
    with pytest.raises(SdoAPIError):
        c.jobs.me()
    c.close()
    assert not injected.is_closed  # the caller owns it
    injected.close()
    assert [r.url.host for r in seen] == ["api.scrape.do", "q.scrape.do"]
    assert seen[1].url.path == "/api/v1/me"
    assert seen[0].extensions["timeout"]["read"] == 3.0


def test_pre_read_response_from_a_custom_transport_is_still_capped():
    injected = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 64))
    )
    with Client(token="t", http_client=injected, max_response_bytes=32) as c:
        with pytest.raises(_transport.SdoResponseTooLargeError):
            c.scrape(U)
    injected.close()


def test_redact_known_covers_registered_tokens_and_shapes():
    from sdo import _logging

    _logging.register_token("LIVE-TOKEN-123")
    _logging.register_token("LIVE-TOKEN-123")
    _logging.unregister_token("LIVE-TOKEN-123")
    assert _logging.redact_known("x LIVE-TOKEN-123 y ?token=abc") == "x *** y ?token=***"
    _logging.unregister_token("LIVE-TOKEN-123")
    assert _logging.redact_known("LIVE-TOKEN-123") == "LIVE-TOKEN-123"


# Scrape.do-Request-Cost is authoritative: any status the gateway billed is the
# target's page, on the scrape and plugin paths alike, and is never retried;
# without a positive cost a status keeps its gateway meaning, and ``billed``
# follows the header when there is one.
@pytest.mark.parametrize("status", [401, 403, 405, 422, 429, 500])
def test_a_billed_target_status_is_a_result_never_retried(status):
    with respx.mock:
        scrape = respx.get(API).mock(
            return_value=httpx.Response(
                status, text="target page", headers={"Scrape.do-Request-Cost": "3"}
            )
        )
        plugin = respx.get("https://api.scrape.do/plugin/google/search").mock(
            return_value=httpx.Response(
                status, content=b"target page", headers={"Scrape.do-Request-Cost": "3"}
            )
        )
        with client(retries=2) as c:
            r = c.scrape(U)
            assert c.plugin("google/search", {"q": "x"}) == b"target page"
    assert (r.status_code, r.text, r.cost) == (status, "target page", 3)
    assert scrape.call_count == 1 and plugin.call_count == 1


@pytest.mark.parametrize(
    ("status", "cost", "cls", "billed", "want_cost"),
    [
        (401, None, SdoAuthError, False, None),
        (401, "0", SdoAuthError, False, 0),
        (400, None, SdoValidationError, False, None),
        (502, "0", SdoGatewayError, False, 0),
        (401, "9007199254740992", SdoAuthError, False, None),
    ],
    ids=["gateway_401", "401_cost_0", "gateway_400", "unbilled_502", "cost_over_max"],
)
def test_an_unbilled_status_keeps_its_gateway_meaning(status, cost, cls, billed, want_cost):
    headers = {} if cost is None else {"Scrape.do-Request-Cost": cost}
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(status, headers=headers))
        with client(retries=0) as c, pytest.raises(cls) as ei:
            c.scrape(U)
    assert ei.value.billed is billed
    assert ei.value.cost == want_cost


@pytest.mark.parametrize(
    ("values", "want"),
    [
        ([], None),
        (["7"], 7),
        ([" 7\t"], 7),
        (["0"], 0),
        (["+7"], None),
        (["-1"], None),
        (["1.5"], None),
        (["٣"], None),
        (["7", "7"], None),
        (["9007199254740991"], 9007199254740991),
        (["9007199254740992"], None),
    ],
)
def test_the_cost_is_read_the_same_way_everywhere(values, want):
    headers = [("scrape.do-request-cost", v) for v in values]
    reply = _transport.Reply(status_code=200, headers=headers)
    assert _transport.request_cost(reply) == want
    assert _transport.target_answered(reply) is (want is not None and want > 0)
    result = ScrapeResult(content=b"", status_code=200, headers=headers, url=U)
    assert result.cost == want
