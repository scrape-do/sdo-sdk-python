# SPDX-License-Identifier: Apache-2.0
import time
import tracemalloc

import pytest

from sdo import SdoTransportError
from sdo._encoding import _LABELS, _MULTI_BYTE, _SINGLE_BYTE, decode, lookup
from sdo.response import ScrapeResult


def make(content=b"", headers=None, status=200, url="https://e.com"):
    return ScrapeResult(content=content, status_code=status, headers=headers or {}, url=url)


def test_bytes_text_json_tri_modal():
    r = make(b'{"a": 1}', {"Content-Type": "application/json"})
    assert r.bytes() == b'{"a": 1}'
    assert r.text == '{"a": 1}'
    assert r.text is r.text  # decoded once
    assert r.json() == {"a": 1}


def test_json_ignores_a_utf8_bom_and_raises_a_transport_error_otherwise():
    assert make(b"\xef\xbb\xbf[1]").json() == [1]
    # Target content is untrusted: a parse failure is an SDK error, and the
    # parser's own exception (which quotes the body) is not chained.
    with pytest.raises(SdoTransportError) as ei:
        make(b"not json").json()
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert make(b'"\xff"').json() == "\ufffd"  # invalid UTF-8 is replaced


@pytest.mark.parametrize(
    "body", [b"[NaN]", b"[-Infinity]", b"[1e400]", b"[-1e999]", b"[" + b"9" * 400 + b"]"]
)
def test_json_rejects_numbers_a_double_cannot_hold(body):
    with pytest.raises(SdoTransportError):
        make(body).json()


def test_json_nesting_too_deep_is_a_transport_error():
    """A RecursionError used to escape the taxonomy."""
    deep = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(SdoTransportError):
        make(deep).json()


def test_json_passes_keyword_arguments_through():
    assert make(b'{"a": 1.5}').json(parse_float=str) == {"a": "1.5"}


def test_header_metadata_typed_accessors():
    r = make(
        b"ok",
        {
            "Scrape.do-Request-Cost": "10",
            "Scrape.do-Remaining-Credits": "-3",
        },
    )
    assert r.cost == 10
    assert r.remaining_credits == -3


@pytest.mark.parametrize("raw", ["²", "١٢", "+5", "1.5", "", "-1"])
def test_numeric_headers_accept_ascii_digits_only(raw):
    """str.isdigit() accepts '²' and '١٢', which int() rejects or misreads; a
    hostile header must degrade to None, never raise."""
    r = make(headers={"scrape.do-request-cost": raw, "scrape.do-initial-status-code": raw})
    assert r.cost is None
    assert r.initial_status_code is None


def test_headers_lowercased_and_multi_values_kept():
    r = make(
        headers=[
            ("Content-Type", "text/html"),
            ("Set-Cookie", "a=1; Path=/"),
            ("set-cookie", "b=2, c"),
        ]
    )
    assert r.content_type == "text/html"
    assert r.headers["set-cookie"] == "a=1; Path=/, b=2, c"  # joined, lossy
    assert r.header_values("SET-COOKIE") == ["a=1; Path=/", "b=2, c"]
    assert r.header_values("x-missing") == []


def test_selector_extracts():
    r = make(b"<html><body><h1>Title</h1></body></html>", {"Content-Type": "text/html"})
    assert r.selector().css("h1::text").get() == "Title"


def test_metadata_accessors_populated():
    r = make(
        headers={
            "scrape.do-initial-status-code": "200",
            "scrape.do-target-url": "https://e.com",
            "scrape.do-resolved-url": "https://e.com/final",
            "scrape.do-cookies": "a=1; b=2",
        }
    )
    assert r.initial_status_code == 200
    assert r.target_url == "https://e.com"
    assert r.resolved_url == "https://e.com/final"
    assert r.cookies == "a=1; b=2"


def test_metadata_accessors_absent_are_none():
    r = make(headers={})
    assert r.initial_status_code is None
    assert r.target_url is None
    assert r.resolved_url is None
    assert r.cookies is None


# -- the WHATWG decode ladder (the corpus pins the headline cases) -------------


def test_every_label_resolves_to_a_decodable_encoding():
    """Each WHATWG label maps to an encoding this module can decode; an
    arbitrary Python codec name never reaches ``codecs``."""
    sample = bytes(range(256))
    for label, enc in _LABELS.items():
        assert (
            enc in _SINGLE_BYTE
            or enc in _MULTI_BYTE
            or enc in ("replacement", "x-user-defined", "gbk", "gb18030")
        )
        if enc != "replacement":
            assert isinstance(decode(sample, f"text/plain; charset={label}"), str)


@pytest.mark.parametrize("label", ["punycode", "rot13", "idna", "unicode_escape", "Koi8-r"])
def test_python_only_codecs_are_not_labels(label):
    assert lookup(label) is None


def test_label_lookup_trims_and_is_case_insensitive():
    assert lookup("  Windows-1252\t") == "windows-1252"
    assert lookup("iso-2022-kr") is None  # "replacement" is ignored


def test_windows_1252_c1_holes_map_to_c1_controls():
    assert decode(b"\x81\x8d\x8f\x90\x9d", "text/plain; charset=windows-1252") == (
        "\x81\x8d\x8f\x90\x9d"
    )


def test_windows_holes_above_0x9f_stay_replacement_characters():
    assert decode(b"\xaa", "text/plain; charset=windows-1253") == "�"


@pytest.mark.parametrize(
    ("body", "label", "text"),
    [
        (b"\xae\xbe", "koi8-u", "ўЎ"),  # WHATWG KOI8-U is KOI8-RU
        (b"\xca", "windows-1255", "ֺ"),
        (b"\x80\xff", "x-user-defined", ""),
        (b"\xa4\x40", "big5", "一"),
        (b"\x88\x62", "big5", "Ê̄"),  # an HKSCS sequence
        (b"\xb0\xa1", "euc-kr", "가"),
        (b"\x81\x30\x81\x30", "gb18030", "\x80"),
        (b'\x1b$B$"\x1b(B', "iso-2022-jp", "あ"),
        (b"\xa4\xa2", "euc-jp", "あ"),
        (b"\x00h", "utf-16be", "h"),
    ],
)
def test_encoding_mappings(body, label, text):
    assert decode(body, f"text/html; charset={label}") == text


def test_empty_charset_parameter_is_ignored():
    assert decode("é".encode(), 'text/html; charset=""') == "é"
    assert decode("é".encode(), "text/html; charset") == "é"


def test_meta_prescan_details():
    # the charset attribute wins over http-equiv; comments are skipped
    assert decode(b"<!-- <meta charset=\"koi8-r\"> --><meta charset='iso-8859-2'>\xb1", "") == (
        "<!-- <meta charset=\"koi8-r\"> --><meta charset='iso-8859-2'>ą"
    )
    # a repeated attribute: the first one counts, as in HTML
    assert decode(b'<meta charset="koi8-r" charset="utf-8">\xc1', "").endswith("а")
    # a <meta> without a charset is skipped
    assert decode(b'<meta name="viewport"><meta charset="koi8-r">\xc1', "").endswith("а")
    # an unknown label is skipped and the next <meta> is used
    body = b'<meta charset="bogus"><meta http-equiv=content-type content="x; charset=koi8-r">\xc1'
    assert decode(body, "text/html").endswith("а")
    # http-equiv without a charset in content, and x-user-defined -> windows-1252
    assert decode(b'<meta http-equiv="content-type" content="text/html">\xe9', "").endswith("�")
    assert decode(b'<meta charset="x-user-defined">\x80', "application/xhtml+xml").endswith("€")
    assert decode(b"<meta charset=\xff>\xe9", "text/html").endswith("�")


def test_punycode_page_decodes_in_linear_time():
    """A page declaring charset=punycode used to reach Python's quadratic
    punycode decoder; now it is an unknown label and UTF-8 is used."""
    body = b'<meta charset="punycode">' + b"-" + b"a" * 2_000_000
    started = time.monotonic()
    labelled = make(body, {"content-type": "text/html; charset=punycode"}).text
    sniffed = make(body, {"content-type": "text/html"}).text
    assert time.monotonic() - started < 2
    assert labelled == sniffed == body.decode("ascii")


# The WHATWG gb18030 decoder (GBK uses it too), computed from the WHATWG index
# files: 0x80 is the euro sign, GB18030-2022 code points, and the standard's
# error recovery (a byte that ends a broken sequence starts the next one).
@pytest.mark.parametrize(
    ("hex_body", "text"),
    [
        ("80", "€"),
        ("ade6", ""),
        ("8140", "丂"),
        ("6bf93321", "k�3!"),
        ("a6d9", "︐"),
        ("8135f437", ""),
        ("8431a530", "�"),
        ("8431a5", "�"),
        ("8431", "�"),
        ("81", "�"),
        ("ff41", "�A"),
        ("8120", "� "),
        ("81ff", "�"),
        ("81308120", "�0� "),
        ("8130ff", "�0�"),
        ("8130813a", "�0�:"),
        ("fe39fe39", "�"),
        ("e3329a35", "\U0010ffff"),
        ("e3329a36", "�"),
        ("a3a0a1a1", "　　"),
        ("d2bbb6fe", "一二"),
        ("41fe39fe3942", "A�B"),  # an unmapped pointer inside a run
    ],
)
@pytest.mark.parametrize("label", ["gbk", "gb18030", "gb2312"])
def test_gb18030_decodes_like_the_whatwg_decoder(hex_body, text, label):
    assert decode(bytes.fromhex(hex_body), f"text/html; charset={label}") == text


def test_gbk_page_decodes_in_bounded_memory():
    """A long run of well-formed GBK bytes used to cost the run regex a
    backtrack point per byte: over 500 MiB of memory for 4 MiB of ASCII."""
    body = b"\xd2\xbb" + b"a" * (4 << 20) + b"\xb6\xfe"
    tracemalloc.start()
    try:
        text = decode(body, "text/html; charset=gbk")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert text == "一" + "a" * (4 << 20) + "二"
    assert peak < 8 * len(body)


def test_json_keeps_finite_floats():
    assert make(b"[1.5, -0.25, 1e300]").json() == [1.5, -0.25, 1e300]


# Content-Type parameters are parsed as the WHATWG MIME Sniffing Standard does.
@pytest.mark.parametrize(
    ("content_type", "text"),
    [
        ("text/plain; charset=iso-8859-2; charset=utf-8", "\x80"),  # the first one wins
        ('text/plain; charset="iso-8859-1', "€"),  # an unterminated quote runs to the end
        ('text/plain; charset="iso-\\8859-1"; x=y', "€"),  # a backslash escapes
        ('text/plain; charset="\\', "�"),
        ("text/plain; charset='iso-8859-1'", "�"),  # single quotes are literal
        ("text/plain; ; charset; charset=; x", "�"),
        ("text/plain; foo=bar; CHARSET = x; charset=iso-8859-1  ", "€"),
        ('text/plain; charset="iso-8859-1"junk; x=y', "€"),
    ],
)
def test_charset_parameter_parsing(content_type, text):
    assert decode(b"\x80", content_type) == text


# The <meta> prescan of the HTML Standard: comments and other tags' attributes
# are skipped, and a construct cut off by the 1024-byte window is not honoured.
@pytest.mark.parametrize(
    ("head", "text"),
    [
        (b'<meta charset="iso-8859-1">', "€"),
        (b"<META CHARSET=ISO-8859-1>", "€"),
        (b"<meta charset='iso-8859-1'/>", "€"),
        (b"<meta/charset=iso-8859-1>", "€"),
        (b"<meta charset = iso-8859-1 >", "€"),
        (b'<!-- <meta charset="iso-8859-1"> -->', "�"),
        (b'<!--> <meta charset="iso-8859-1">', "€"),  # "<!-->" is a whole comment
        (b"<!-- never closed <meta charset=iso-8859-1>", "�"),
        (b'<div title="<meta charset=iso-8859-1>">', "�"),
        (b'<div a b=c d="e" f=\'g\' h = i><meta charset="iso-8859-1">', "€"),
        (b"<!doctype html><?php x ?></p ><meta charset=iso-8859-1>", "€"),
        (b"<!doctype never closed <meta charset=iso-8859-1>", "�"),
        (b"</ p><meta charset=iso-8859-1>", "€"),
        (b"<meta charset=nope><meta charset=iso-8859-1>", "€"),
        (b"<meta charset=iso-8859-1 charset=utf-8>", "€"),  # duplicate attributes ignored
        (b'<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">', "€"),
        (b"<meta content=\"text/html; charset='iso-8859-1'\" http-equiv=content-type>", "€"),
        (b'<meta content="charset = iso-8859-1" http-equiv="content-type">', "€"),
        (b'<meta content="charsetx charset=iso-8859-1" http-equiv="content-type">', "€"),
        (b'<meta content="text/html; charset=iso-8859-1">', "�"),  # no pragma
        (b'<meta http-equiv="content-type" content="text/html">', "�"),
        (b'<meta http-equiv="content-type" content="charset=">', "�"),
        (b'<meta http-equiv="content-type" content="charset=\'x">', "�"),
        (b'<meta http-equiv="content-type" content="charset=nope">', "�"),
        (b'<meta http-equiv="content-type" content="charset">', "�"),
        (b"<meta charset=x-user-defined>", "€"),  # means windows-1252 in HTML
        (b'<meta name="x" charset', "�"),  # runs out of bytes
        (b"<meta charset=iso-8859-1", "�"),
        (b'<meta charset="iso-8859-1', "�"),
        (b"<meta", "�"),
        (b"<b", "�"),
        (b"<", "�"),
        (b"<3 <meta charset=iso-8859-1>", "€"),
        (b"<meta foo/><meta charset=iso-8859-1>", "€"),
        (b"<meta charset=><meta charset=iso-8859-1>", "€"),
        (b"<?x", "\ufffd"),
    ],
)
def test_meta_prescan(head, text):
    body = head + b"\x80"
    for content_type in ("text/html", None):
        assert decode(body, content_type)[-1] == text


def test_meta_cut_by_the_1024_byte_window_is_ignored():
    head = b" " * 998 + b'<meta charset="iso-8859-1">'  # ">" is byte 1025
    assert decode(head + b"\x80", "text/html")[-1] == "�"
    head = b" " * 996 + b'<meta charset="iso-8859-1">'
    assert decode(head + b"\x80", "text/html")[-1] == "€"


def test_meta_label_matching_is_ascii_only():
    kelvin = '<meta charset="Koi8-r">'.encode()
    assert decode(kelvin + b"\xe9", "text/html")[-1] == "�"
