# SPDX-License-Identifier: Apache-2.0
"""The bounded Content-Encoding decoder, driven directly: every chunking of
every coding must round-trip, and no decoder call may exceed its step."""

import gzip
import logging
import os
import zlib

import brotli
import httpx
import pytest
import respx

from sdo import Client, SdoTransportError, _decompress, _logging
from sdo._decompress import BodyDecoder, DecodeError, TooLarge, codings
from sdo._wire import es_number

PAYLOAD = os.urandom(3000) + b"abc" * 5000 + b"\0" * 20000


def _raw_deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return c.compress(data) + c.flush()


ENCODED = {
    "gzip": gzip.compress(PAYLOAD),
    "x-gzip": gzip.compress(PAYLOAD),
    "deflate": zlib.compress(PAYLOAD),
    "deflate ": _raw_deflate(PAYLOAD),
    "br": brotli.compress(PAYLOAD),
    "gzip, br": brotli.compress(gzip.compress(PAYLOAD)),
    "identity": PAYLOAD,
    "": PAYLOAD,
}


_LIMIT = 10 * len(PAYLOAD)


def decode(coding, blob, chunk, limit=_LIMIT):
    d = BodyDecoder(coding, limit)
    for i in range(0, len(blob), chunk):
        d.feed(blob[i : i + chunk])
    return d.finish()


@pytest.fixture
def tiny_step(monkeypatch):
    """A 7-byte step makes output caps and drains happen on every call."""
    monkeypatch.setattr(_decompress, "_STEP", 7)


@pytest.mark.parametrize(
    ("coding", "chunk"),
    [
        (coding, chunk)
        for coding in sorted(ENCODED)
        for chunk in (1, 3, 4096, 1 << 30)
        if chunk > 1 or len(ENCODED[coding]) < 20000  # byte-at-a-time only when small
    ],
)
def test_round_trip_with_tiny_steps(tiny_step, coding, chunk):
    assert decode(coding, ENCODED[coding], chunk) == PAYLOAD


@pytest.mark.parametrize("chunk", range(1, 12))
def test_gzip_members_are_concatenated_and_garbage_after_them_is_ignored(chunk):
    blob = gzip.compress(b"first") + gzip.compress(b"second") + b"\x1f"
    assert decode("gzip", blob, chunk) == b"firstsecond"
    assert decode("gzip", gzip.compress(b"x") + b"trailing garbage", chunk) == b"x"


def test_trailing_bytes_after_a_deflate_stream_are_ignored():
    assert decode("deflate", zlib.compress(b"abc") + b"junk", 2) == b"abc"


@pytest.mark.parametrize("chunk", [1, 5, 64, 4096, 1 << 30])
@pytest.mark.parametrize("coding", ["br", "gzip, br"])
@pytest.mark.parametrize("step", [7, 64 * 1024])
def test_trailing_bytes_after_a_brotli_stream_are_ignored_whatever_the_chunking(
    chunk, coding, step, monkeypatch
):
    monkeypatch.setattr(_decompress, "_STEP", step)
    blob = brotli.compress(PAYLOAD) + b"junk" * 50
    if coding == "gzip, br":
        blob = brotli.compress(gzip.compress(PAYLOAD)) + b"junk"
    assert decode(coding, blob, chunk) == PAYLOAD


def test_brotli_corruption_after_valid_output_is_still_an_error(tiny_step):
    blob = brotli.compress(PAYLOAD)
    for broken in (blob[:200] + b"\xff" * 64 + blob[264:], blob[:-3] + b"\xff\xff\xff"):
        for chunk in (7, 1 << 30):
            with pytest.raises(DecodeError):
                decode("br", broken, chunk)


def test_brotli_trailing_bytes_after_a_bomb_still_hit_the_cap():
    bomb = brotli.compress(b"\0" * 5_000_000) + b"junk"
    with pytest.raises(TooLarge):
        decode("br", bomb, 1 << 30, limit=100_000)


def test_short_and_empty_bodies():
    assert decode("deflate", b"", 1) == b""
    assert decode("gzip", b"", 1) == b""
    assert decode("br", b"", 1) == b""
    # a stream cut short is an error, however it was chunked
    for coding, blob in (
        ("deflate", b"\x78"),
        ("deflate", zlib.compress(b"abcdef" * 100)[:-3]),
        ("gzip", gzip.compress(b"abcdef" * 100)[:-12]),
        ("br", brotli.compress(b"abcdef" * 100)[:-1]),
        ("gzip", gzip.compress(b"a") + b"\x1f\x8b"),  # a second member cut short
    ):
        for chunk in (1, 7, 1 << 30):
            with pytest.raises(DecodeError):
                decode(coding, blob, chunk)


def test_trailing_bytes_after_the_last_gzip_member_are_ignored():
    for tail in (b"\x00", b"\x00garbage", b"\x1f"):
        assert decode("gzip", gzip.compress(b"abc") + tail, 1) == b"abc"


def test_partial_keeps_the_prefix_that_fits_under_the_cap():
    d = BodyDecoder("gzip", 10)
    with pytest.raises(TooLarge):
        d.feed(gzip.compress(b"0123456789abcdef"))
    assert d.partial() == b"0123456789"


@pytest.mark.parametrize("coding", ["gzip", "deflate", "br"])
def test_corrupt_streams_raise_decode_error(coding):
    with pytest.raises(DecodeError):
        decode(coding, b"\x78\x9c\xff\xff\xff\xff definitely not " + coding.encode(), 5)


def test_every_stage_is_capped():
    inner = gzip.compress(b"\0" * 100_000)
    with pytest.raises(TooLarge):
        decode("gzip", inner, 1024, limit=50_000)
    # the outer stage alone may cross the cap first
    with pytest.raises(TooLarge):
        decode("gzip, br", brotli.compress(gzip.compress(os.urandom(60_000))), 1024, limit=50_000)


def test_codings_parsing():
    assert codings(None) == []
    assert codings(" GZIP , identity, br ") == ["br", "gzip"]
    # undone from the last coding; an unsupported one stops there
    assert codings("gzip, zstd") == []
    assert codings("zstd, gzip") == ["gzip"]
    assert codings("x-gzip, deflate") == ["deflate", "gzip"]


def test_at_most_five_codings_are_decoded():
    body = b"hello"
    for _ in range(6):
        body = gzip.compress(body)
    five = gzip.decompress(body)
    assert decode(", ".join(["gzip"] * 5), five, 1) == b"hello"
    # a longer chain is an error whatever the body; nothing is decoded
    for header in (", ".join(["gzip"] * 6), "gzip," * 2000, "br, deflate, gzip, x-gzip, br, gzip"):
        d = BodyDecoder(header, _LIMIT)
        with pytest.raises(DecodeError, match="^more than 5 content codings$"):
            d.feed(body)
        assert d.partial() == b""
        with pytest.raises(DecodeError):
            BodyDecoder(header, _LIMIT).finish()
    # identity and the codings after an unsupported one are not counted
    assert decode("gzip" + ", identity" * 5, gzip.compress(b"x"), 1) == b"x"
    assert decode("gzip, gzip, gzip, gzip, gzip, gzip, zstd", b"raw", 1) == b"raw"


@respx.mock
def test_too_many_codings_is_a_transport_error_that_is_not_retried():
    body = b"hello"
    for _ in range(6):
        body = gzip.compress(body)

    class Raw(httpx.SyncByteStream):
        def __iter__(self):
            yield body

    respx.get("https://api.scrape.do/").mock(
        return_value=httpx.Response(200, stream=Raw(), headers={"Content-Encoding": "gzip," * 6})
    )
    with Client(token="t", retries=2, backoff_factor=0.0) as c:
        with pytest.raises(SdoTransportError) as ei:
            c.scrape("https://e.com")
    assert type(ei.value) is SdoTransportError
    assert "more than 5 content codings" in str(ei.value)
    assert respx.calls.call_count == 1


def test_es_number_refuses_non_numbers():
    with pytest.raises(TypeError):
        es_number(True)
    with pytest.raises(ValueError):
        es_number(float("inf"))


def test_log_filter_is_installed_once():
    _logging.install()
    _logging.install()
    for name in ("httpx", "httpcore.http11"):
        filters = logging.getLogger(name).filters
        assert sum(type(f).__name__ == "RedactingFilter" for f in filters) == 1
