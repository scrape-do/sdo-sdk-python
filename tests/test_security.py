# SPDX-License-Identifier: Apache-2.0
"""Security invariants. Each test pins a guarantee the SDK documents (the token
is never logged or echoed, decompression is bounded, caller input cannot
reshape the request) that would otherwise only be checked by reading the code.
A divergence here is a release blocker."""

import gzip
import logging
import tracemalloc
import zlib

import brotli
import httpx
import pytest
import respx

import sdo
from sdo import Client, SdoAuthError, SdoConfigError, SdoError, SdoResponseTooLargeError
from sdo.client import _user_agent

TOKEN = "SUPERSECRETTOKEN"
API = "https://api.scrape.do/"
U = "https://e.com"


class Raw(httpx.SyncByteStream):
    """Serve bytes in network-sized chunks without httpx reading them first."""

    def __init__(self, data: bytes, chunk: int = 64 * 1024) -> None:
        self.data, self.chunk = data, chunk

    def __iter__(self):
        for i in range(0, len(self.data), self.chunk):
            yield self.data[i : i + self.chunk]


def _all_text(exc: BaseException) -> str:
    """Everything a caller could plausibly log from an exception."""
    parts = [str(exc), repr(exc), getattr(exc, "body", "") or ""]
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            parts += [str(linked), repr(linked)]
    return " ".join(parts)


# -- the token never reaches a log record -------------------------------------


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_token_absent_from_httpx_logs(caplog, level):
    """httpx logs 'HTTP Request: GET <url>' at INFO, and the sync URL carries
    the token; scrape, plugin and jobs records must all be masked."""
    caplog.set_level(level)
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, text="ok"))
        respx.get(f"{API}plugin/google/search").mock(return_value=httpx.Response(200, json={}))
        respx.get("https://q.scrape.do/api/v1/me").mock(return_value=httpx.Response(200, json={}))
        with Client(token=TOKEN) as c:
            c.scrape(U)
            c.plugin("google/search", {"q": "x"})
            c.jobs.me()
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("httpx")]
    assert len(messages) == 3
    assert all(TOKEN not in m for m in messages)
    assert "token=***" in messages[0]


def test_token_absent_from_httpcore_debug_records(caplog):
    """httpcore traces at DEBUG; anything echoing the token is masked too."""
    caplog.set_level(logging.DEBUG)
    with Client(token=TOKEN):
        logging.getLogger("httpcore.http11").debug(
            "receive_response_headers.complete %s", [(b"X-Echo", f"X-Token: {TOKEN}".encode())]
        )
        logging.getLogger("httpcore.connection").debug("echo %s", TOKEN)
        logging.getLogger("httpcore.proxy").debug("bad format %s %s", "one-arg-only")
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_token_absent_from_hpack_debug_records(caplog):
    """With httpx[http2], hpack logs header blocks and evicted header-table
    entries (X-Token among them) at DEBUG."""
    caplog.set_level(logging.DEBUG)
    with Client(token=TOKEN):
        logging.getLogger("hpack.hpack").debug("Decoded %s, consumed %d", ("x-token", TOKEN), 9)
        logging.getLogger("hpack.table").debug("Evicting %s: %s", "x-token", TOKEN)
        logging.getLogger("hpack").debug("Encoded header block to %s", TOKEN.encode())
    assert len(caplog.records) == 3
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_filter_leaves_unrelated_records_untouched(caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger("httpx").info("plain %s", "message")
    (record,) = caplog.records
    assert (record.msg, record.args) == ("plain %s", ("message",))


# -- the token never reaches an error ---------------------------------------------


def test_token_absent_from_transport_error():
    with respx.mock:
        respx.get(API).mock(side_effect=httpx.ConnectError(f"refused {API}?token={TOKEN}"))
        with Client(token=TOKEN, retries=0) as c, pytest.raises(SdoError) as ei:
            c.scrape(U)
    assert TOKEN not in _all_text(ei.value)


def test_token_absent_from_timeout_error():
    with respx.mock:
        respx.get(API).mock(side_effect=httpx.ReadTimeout(f"slow {TOKEN}"))
        with Client(token=TOKEN, retries=0) as c, pytest.raises(SdoError) as ei:
            c.scrape(U)
    assert TOKEN not in _all_text(ei.value)


@pytest.mark.parametrize(
    "echo",
    [
        f"Invalid token {TOKEN} provided",  # a bare echo: only a literal scrub catches it
        f'{{"Message": "denied", "token": "{TOKEN}"}}',
        f"X-Token: {TOKEN}",
        "token%3D" + TOKEN,
    ],
)
def test_token_echoed_by_the_api_is_masked(echo):
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(401, text=echo))
        with Client(token=TOKEN, retries=0) as c, pytest.raises(SdoError) as ei:
            c.scrape(U)
    assert TOKEN not in _all_text(ei.value)


def test_token_echoed_in_the_error_envelope_is_masked():
    with respx.mock:
        respx.get(API).mock(
            return_value=httpx.Response(400, json={"Message": [f"bad token {TOKEN}"]})
        )
        with Client(token=TOKEN, retries=0) as c, pytest.raises(SdoError) as ei:
            c.scrape(U)
    assert ei.value.message == "bad token ***"


def test_redaction_covers_urls_the_sdk_did_not_build():
    """Credential shapes are masked whatever their source."""
    from sdo.errors import redact_secrets

    assert redact_secrets("GET https://api.scrape.do/?token=abc123&url=x failed") == (
        "GET https://api.scrape.do/?token=***&url=x failed"
    )
    assert redact_secrets("?api_key=zzz") == "?api_key=***"
    assert redact_secrets("&Access-Token=zzz end") == "&Access-Token=*** end"
    assert redact_secrets("nothing to redact") == "nothing to redact"
    assert redact_secrets("id \ud800 SECRET12", "\ud800 SECRET12") == "id ***"


def test_repr_never_shows_the_token():
    c = Client(token=TOKEN)
    assert TOKEN not in repr(c) and TOKEN not in str(vars(c).get("base_url"))
    c.close()


# -- the token only travels over https ---------------------------------------------


@pytest.mark.parametrize("url", ["http://api.scrape.do", "http://localhost.evil.com"])
def test_plain_http_base_url_is_refused(url, monkeypatch):
    with pytest.raises(SdoConfigError, match="https"):
        Client(token=TOKEN, base_url=url)
    monkeypatch.setenv("SDO_ASYNC_BASE_URL", url)
    with pytest.raises(SdoConfigError, match="https"):
        Client(token=TOKEN)


def test_default_client_ignores_proxy_and_ca_environment(monkeypatch):
    """A planted HTTPS_PROXY / SSL_CERT_FILE must not route the token through a
    proxy or swap the trust store; an injected client keeps its own settings."""
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.example:8080")
    with Client(token=TOKEN) as c:
        own = c._transport._client
        assert own.trust_env is False
        assert own._mounts == {}  # no proxy transport was mounted from the environment
    injected = httpx.Client()
    with Client(token=TOKEN, http_client=injected) as c:
        assert c._transport._client is injected and injected.trust_env is True
    injected.close()


# -- bounded decompression -------------------------------------------------------------


def _zlib(wbits: int, size: int = 64 * 1024 * 1024) -> bytes:
    """``size`` zero bytes, compressed without holding them in memory."""
    c = zlib.compressobj(9, zlib.DEFLATED, wbits)
    block = b"\0" * (1024 * 1024)
    return b"".join(c.compress(block) for _ in range(size // len(block))) + c.flush()


def _brotli(size: int = 64 * 1024 * 1024) -> bytes:
    c = brotli.Compressor(quality=5)
    block = b"\0" * (1024 * 1024)
    return b"".join(c.process(block) for _ in range(size // len(block))) + c.finish()


_BOMBS = {
    "gzip": ("gzip", _zlib(16 + zlib.MAX_WBITS)),
    "deflate-zlib": ("deflate", _zlib(zlib.MAX_WBITS)),
    "deflate-raw": ("deflate", _zlib(-zlib.MAX_WBITS)),
    "br": ("br", _brotli()),
    "gzip-in-br": ("gzip, br", brotli.compress(_zlib(16 + zlib.MAX_WBITS))),
}


@pytest.mark.parametrize("name", sorted(_BOMBS))
def test_decompression_bomb_is_refused_without_materializing(name):
    """64 MiB of zeros compresses to a few KiB. It must abort at the 1 MiB cap
    with memory bounded near the cap, not the inflated size (httpx's brotli
    decoder used to inflate a whole network chunk in one unbounded call)."""
    coding, bomb = _BOMBS[name]
    assert len(bomb) < 100_000, "fixture should be small compressed"
    limit = 1024 * 1024
    with respx.mock:
        respx.get(API).mock(
            return_value=httpx.Response(200, stream=Raw(bomb), headers={"content-encoding": coding})
        )
        with Client(token=TOKEN, retries=0, max_response_bytes=limit) as c:
            tracemalloc.start()
            try:
                with pytest.raises(SdoResponseTooLargeError) as ei:
                    c.scrape(U)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
    assert ei.value.limit == limit
    assert peak < 8 * limit, f"peak {peak} bytes"


def test_bomb_on_an_error_status_keeps_the_status_and_the_jobs_path_is_refused():
    """An error body is only read up to 64 KiB, so a bomb there cannot inflate,
    and the status still decides the error."""
    coding, bomb = _BOMBS["br"]
    headers = {"content-encoding": coding}
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(403, stream=Raw(bomb), headers=headers))
        respx.get("https://q.scrape.do/api/v1/me").mock(
            return_value=httpx.Response(200, stream=Raw(bomb), headers=headers)
        )
        with Client(token=TOKEN, retries=0, max_response_bytes=1024 * 1024) as c:
            with pytest.raises(SdoAuthError) as ei:
                c.scrape(U)
            assert ei.value.status_code == 403 and len(ei.value.body or "") == 2048
            with pytest.raises(SdoResponseTooLargeError):
                c.jobs.me()


def test_body_under_the_cap_still_decompresses():
    payload = b"<html>" + b"x" * 5000 + b"</html>"
    with respx.mock:
        respx.get(API).mock(
            return_value=httpx.Response(
                200, stream=Raw(gzip.compress(payload), 7), headers={"content-encoding": "gzip"}
            )
        )
        with Client(token=TOKEN, retries=0, max_response_bytes=1024 * 1024) as c:
            assert c.scrape(U).bytes() == payload


# -- caller input cannot reshape the request ----------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        "../",
        "..%2f..",
        "x#",
        "x?token=evil",
        "a/../b",
        "Google/Shopping",
        "",
        "  ",
        "google/search\n",
    ],
)
def test_plugin_route_rejected_before_any_request(route):
    """A bad route must never reach the wire; tools pass model-chosen values."""
    with respx.mock(assert_all_called=False) as mock, Client(token=TOKEN, retries=0) as c:
        with pytest.raises(SdoConfigError):
            c.plugin(route, {"q": "1"})
        assert mock.calls.call_count == 0


@pytest.mark.parametrize("job_id", ["../me", "a?b=c", "a#b", "../../jobs"])
def test_job_id_cannot_reach_another_endpoint(job_id):
    with respx.mock(assert_all_called=False) as mock, Client(token=TOKEN) as c:
        with pytest.raises(SdoConfigError):
            c.jobs.cancel(job_id)
        assert mock.calls.call_count == 0


def test_async_redirect_is_an_error_and_never_carries_the_token_away():
    with respx.mock:
        respx.get("https://q.scrape.do/api/v1/me").mock(
            return_value=httpx.Response(307, headers={"Location": "https://attacker.test/"})
        )
        with Client(token=TOKEN, retries=0) as c, pytest.raises(SdoError):
            c.jobs.me()
        assert all(call.request.url.host == "q.scrape.do" for call in respx.calls)


# -- User-Agent is the whole client-identifying surface ----------------------------------


def test_user_agent_is_exactly_name_and_version():
    """No OS, no interpreter version: the UA is `sdo-sdk-<lang>/<version>`."""
    import sdo

    assert _user_agent() == f"sdo-sdk-python/{sdo.__version__}"


#: Hosts allowed to appear as a URL literal in the shipped package: the
#: customer's own gateway, plus the documentation placeholders used in examples.
_ALLOWED_HOSTS = {"api.scrape.do", "q.scrape.do", "scrape.do", "example.com"}


def test_no_network_egress_beyond_the_gateway():
    """The only host the SDK contacts is the customer's own Scrape.do endpoint —
    no registry pings, no update checks, no telemetry."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "sdo"
    found = set()
    for path in src.glob("*.py"):
        for host in re.findall(r"""["']https?://([^/"'\s{]+)""", path.read_text("utf-8")):
            found.add(host)
    assert found <= _ALLOWED_HOSTS, f"unexpected outbound hosts: {found - _ALLOWED_HOSTS}"


# -- the token is not in any frame an error tracker records ---------------------------


class _Boom(httpx.BaseTransport):
    """A custom transport that raises a non-httpx error quoting the URL."""

    def __init__(self, exc_type: type[Exception]) -> None:
        self.exc_type = exc_type

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc_type(f"cannot reach {request.url} (X-Token: {TOKEN})")


class _Drip(httpx.SyncByteStream):
    def __iter__(self):
        import time

        for _ in range(50):
            time.sleep(0.01)
            yield b"x"


def _frames_hold_no_token(exc: BaseException) -> None:
    """What Sentry's include_local_variables, pytest --showlocals and
    traceback's capture_locals record: repr() of every local of every frame
    the exception passed through."""
    import traceback
    from urllib.parse import quote

    forms = (TOKEN, quote(TOKEN, safe=""))
    tb = exc.__traceback__
    assert tb is not None
    while tb is not None:
        for name, value in tb.tb_frame.f_locals.items():
            shown = repr(value)
            assert not any(f in shown for f in forms), (tb.tb_frame.f_code.co_name, name)
        tb = tb.tb_next
    text = "".join(traceback.TracebackException.from_exception(exc, capture_locals=True).format())
    assert not any(f in text for f in forms)


def _raises(call) -> SdoError:
    try:
        call()
    except SdoError as exc:
        return exc
    raise AssertionError("no SdoError raised")


def test_no_frame_holds_the_token_when_an_error_is_raised():
    """sec: every raise path — API error, transport error, timeout, size cap,
    closed client, config errors — scrape, plugin and jobs alike."""
    errors = []
    with respx.mock(assert_all_called=False) as mock:
        mock.get(API).mock(return_value=httpx.Response(502, text="upstream"))
        mock.get(f"{API}plugin/google/search").mock(
            return_value=httpx.Response(200, text="{", headers={"Content-Type": "application/json"})
        )
        mock.get(f"{API}plugin/google/fail").mock(side_effect=httpx.ConnectError(f"token={TOKEN}"))
        mock.get("https://q.scrape.do/api/v1/me").mock(return_value=httpx.Response(404))
        mock.get("https://q.scrape.do/api/v1/jobs").mock(return_value=httpx.Response(200, json=[]))
        mock.get("https://q.scrape.do/api/v1/jobs/j").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("https://api.scrape.do/plugin/big/one").mock(
            return_value=httpx.Response(200, stream=Raw(b"x" * 4096))
        )
        with Client(token=TOKEN, retries=0, max_response_bytes=1024) as c:
            errors += [
                _raises(lambda: c.scrape(U)),
                _raises(lambda: c.scrape(U, headers={"Host": "x"})),
                _raises(lambda: c.scrape("ftp://e.com")),
                _raises(lambda: c.scrape(U, bogus=1)),
                _raises(lambda: c.plugin("google/search")),
                _raises(lambda: c.plugin("google/fail")),
                _raises(lambda: c.plugin("google/search", {"Token": "x"})),
                _raises(lambda: c.plugin("big/one")),
                _raises(lambda: c.plugin("google/nomock")),  # respx: not mocked
                _raises(lambda: c.jobs.me()),
                _raises(lambda: c.jobs.list()),
                _raises(lambda: c.jobs.status("j")),
                _raises(lambda: c.jobs.create([U], "not a mapping")),  # type: ignore[arg-type]
            ]
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, stream=_Drip()))
        with Client(token=TOKEN, timeout=0.05, retries=0) as c:
            errors.append(_raises(lambda: c.scrape(U)))
    for exc_type in (RuntimeError, OSError):
        injected = httpx.Client(transport=_Boom(exc_type))
        with Client(token=TOKEN, http_client=injected, retries=0) as c:
            errors.append(_raises(lambda: c.scrape(U)))
            errors.append(_raises(lambda: c.jobs.me()))
        injected.close()
    closed = Client(token=TOKEN)
    closed.close()
    errors.append(_raises(lambda: closed.scrape(U)))
    errors += [
        _raises(lambda: Client(token=TOKEN, base_url="http://evil.example")),
        _raises(lambda: Client(token=TOKEN + " x")),
        _raises(lambda: Client(token=TOKEN, timeout=-1)),
        _raises(lambda: sdo.AsyncJobs(token=TOKEN, base_url="http://evil.example")),
    ]
    assert len(errors) == 23
    for err in errors:
        assert TOKEN not in _all_text(err)
        _frames_hold_no_token(err)


def test_injected_transport_errors_stay_in_the_taxonomy():
    for exc_type in (RuntimeError, OSError, ValueError):
        injected = httpx.Client(transport=_Boom(exc_type))
        with Client(token=TOKEN, http_client=injected, retries=2) as c:
            with pytest.raises(SdoError) as ei:
                c.scrape(U)
        injected.close()
        assert type(ei.value).__name__ == "SdoTransportError"
        assert exc_type.__name__ in ei.value.message and TOKEN not in ei.value.message


def test_an_interrupt_mid_request_leaves_no_token_in_its_frames():
    """KeyboardInterrupt is not the SDK's to catch, but its traceback passes
    through the frame that built the request."""
    injected = httpx.Client(transport=_Boom(KeyboardInterrupt))
    with Client(token=TOKEN, http_client=injected, retries=0) as c:
        for call in (lambda: c.scrape(U), lambda: c.jobs.me()):
            with pytest.raises(KeyboardInterrupt) as ei:
                call()
            tb = ei.value.__traceback__
            frames = []
            while tb is not None:
                frames.append(tb.tb_frame.f_code.co_name)
                for name, value in tb.tb_frame.f_locals.items():
                    if "/sdo/" in tb.tb_frame.f_code.co_filename.replace("\\", "/"):
                        assert TOKEN not in repr(value), name
                tb = tb.tb_next
            assert "_attempt" in frames
    injected.close()


def test_an_interrupt_mid_request_leaves_no_token_in_any_frame_below_the_sdk():
    """The frames below the SDK (httpx's send path, the transport's
    handle_request) hold the request and its token URL; they are cleared while
    the interrupt keeps its type and its traceback keeps its shape."""
    from urllib.parse import quote

    forms = (TOKEN, quote(TOKEN, safe=""))

    class _Interrupt(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise KeyboardInterrupt

    injected = httpx.Client(transport=_Interrupt())
    with Client(token=TOKEN, http_client=injected, retries=0) as c:
        for call in (lambda: c.scrape(U), lambda: c.plugin("google/search"), lambda: c.jobs.me()):
            with pytest.raises(KeyboardInterrupt) as ei:
                call()
            tb = ei.value.__traceback__.tb_next  # below this test's own frame
            frames = []
            while tb is not None:
                frames.append(tb.tb_frame.f_code.co_name)
                for name, value in tb.tb_frame.f_locals.items():
                    shown = repr(value)
                    assert not any(f in shown for f in forms), (tb.tb_frame.f_code.co_name, name)
                tb = tb.tb_next
            assert frames[-1] == "handle_request" and "_attempt" in frames
    injected.close()
