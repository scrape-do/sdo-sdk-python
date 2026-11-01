# SPDX-License-Identifier: Apache-2.0
"""The WHATWG Encoding Standard "decode" ladder for ``ScrapeResult.text``.

JavaScript (``TextDecoder``) and Go (``htmlindex``) accept only the labels of the
Encoding Standard; Python's ``codecs`` accepts hundreds of names, including
codecs that are not text encodings at all and one (``punycode``) whose decoder
is quadratic. A page controls the label it declares, so a label is never passed
to ``codecs`` directly: it is resolved through the WHATWG table below and
mapped to a Python codec (or a decode table) chosen here.

Ladder: BOM > Content-Type ``charset`` > ``*/json`` > HTML ``<meta>`` in the
first 1024 bytes > UTF-8. An unknown label, or one that maps to WHATWG
"replacement", is ignored and the ladder continues.

Exactness: UTF-8, UTF-16, every single-byte encoding and gb18030/GBK follow the
WHATWG decoders byte for byte. Big5, EUC-JP, ISO-2022-JP, Shift_JIS and EUC-KR
use the closest Python codec: well-formed text decodes as WHATWG specifies
(except 203 HKSCS-2008 Big5 additions Python does not map), but recovery from
malformed bytes may place U+FFFD differently.
"""

from __future__ import annotations

import re

# -- labels -> WHATWG encoding names (https://encoding.spec.whatwg.org/#names-and-labels)

_LABELS_BY_ENCODING: dict[str, tuple[str, ...]] = {
    "utf-8": (
        "unicode-1-1-utf-8",
        "unicode11utf8",
        "unicode20utf8",
        "utf-8",
        "utf8",
        "x-unicode20utf8",
    ),
    "ibm866": ("866", "cp866", "csibm866", "ibm866"),
    "iso-8859-2": (
        "csisolatin2",
        "iso-8859-2",
        "iso-ir-101",
        "iso8859-2",
        "iso88592",
        "iso_8859-2",
        "iso_8859-2:1987",
        "l2",
        "latin2",
    ),
    "iso-8859-3": (
        "csisolatin3",
        "iso-8859-3",
        "iso-ir-109",
        "iso8859-3",
        "iso88593",
        "iso_8859-3",
        "iso_8859-3:1988",
        "l3",
        "latin3",
    ),
    "iso-8859-4": (
        "csisolatin4",
        "iso-8859-4",
        "iso-ir-110",
        "iso8859-4",
        "iso88594",
        "iso_8859-4",
        "iso_8859-4:1988",
        "l4",
        "latin4",
    ),
    "iso-8859-5": (
        "csisolatincyrillic",
        "cyrillic",
        "iso-8859-5",
        "iso-ir-144",
        "iso8859-5",
        "iso88595",
        "iso_8859-5",
        "iso_8859-5:1988",
    ),
    "iso-8859-6": (
        "arabic",
        "asmo-708",
        "csiso88596e",
        "csiso88596i",
        "csisolatinarabic",
        "ecma-114",
        "iso-8859-6",
        "iso-8859-6-e",
        "iso-8859-6-i",
        "iso-ir-127",
        "iso8859-6",
        "iso88596",
        "iso_8859-6",
        "iso_8859-6:1987",
    ),
    "iso-8859-7": (
        "csisolatingreek",
        "ecma-118",
        "elot_928",
        "greek",
        "greek8",
        "iso-8859-7",
        "iso-ir-126",
        "iso8859-7",
        "iso88597",
        "iso_8859-7",
        "iso_8859-7:1987",
        "sun_eu_greek",
    ),
    "iso-8859-8": (
        "csiso88598e",
        "csisolatinhebrew",
        "hebrew",
        "iso-8859-8",
        "iso-8859-8-e",
        "iso-ir-138",
        "iso8859-8",
        "iso88598",
        "iso_8859-8",
        "iso_8859-8:1988",
        "visual",
    ),
    "iso-8859-8-i": ("csiso88598i", "iso-8859-8-i", "logical"),
    "iso-8859-10": (
        "csisolatin6",
        "iso-8859-10",
        "iso-ir-157",
        "iso8859-10",
        "iso885910",
        "l6",
        "latin6",
    ),
    "iso-8859-13": ("iso-8859-13", "iso8859-13", "iso885913"),
    "iso-8859-14": ("iso-8859-14", "iso8859-14", "iso885914"),
    "iso-8859-15": (
        "csisolatin9",
        "iso-8859-15",
        "iso8859-15",
        "iso885915",
        "iso_8859-15",
        "l9",
    ),
    "iso-8859-16": ("iso-8859-16",),
    "koi8-r": ("cskoi8r", "koi", "koi8", "koi8-r", "koi8_r"),
    "koi8-u": ("koi8-ru", "koi8-u"),
    "macintosh": ("csmacintosh", "mac", "macintosh", "x-mac-roman"),
    "windows-874": (
        "dos-874",
        "iso-8859-11",
        "iso8859-11",
        "iso885911",
        "tis-620",
        "windows-874",
    ),
    "windows-1250": ("cp1250", "windows-1250", "x-cp1250"),
    "windows-1251": ("cp1251", "windows-1251", "x-cp1251"),
    "windows-1252": (
        "ansi_x3.4-1968",
        "ascii",
        "cp1252",
        "cp819",
        "csisolatin1",
        "ibm819",
        "iso-8859-1",
        "iso-ir-100",
        "iso8859-1",
        "iso88591",
        "iso_8859-1",
        "iso_8859-1:1987",
        "l1",
        "latin1",
        "us-ascii",
        "windows-1252",
        "x-cp1252",
    ),
    "windows-1253": ("cp1253", "windows-1253", "x-cp1253"),
    "windows-1254": (
        "cp1254",
        "csisolatin5",
        "iso-8859-9",
        "iso-ir-148",
        "iso8859-9",
        "iso88599",
        "iso_8859-9",
        "iso_8859-9:1989",
        "l5",
        "latin5",
        "windows-1254",
        "x-cp1254",
    ),
    "windows-1255": ("cp1255", "windows-1255", "x-cp1255"),
    "windows-1256": ("cp1256", "windows-1256", "x-cp1256"),
    "windows-1257": ("cp1257", "windows-1257", "x-cp1257"),
    "windows-1258": ("cp1258", "windows-1258", "x-cp1258"),
    "x-mac-cyrillic": ("x-mac-cyrillic", "x-mac-ukrainian"),
    "gbk": (
        "chinese",
        "csgb2312",
        "csiso58gb231280",
        "gb2312",
        "gb_2312",
        "gb_2312-80",
        "gbk",
        "iso-ir-58",
        "x-gbk",
    ),
    "gb18030": ("gb18030",),
    "big5": ("big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5"),
    "euc-jp": ("cseucpkdfmtjapanese", "euc-jp", "x-euc-jp"),
    "iso-2022-jp": ("csiso2022jp", "iso-2022-jp"),
    "shift_jis": (
        "csshiftjis",
        "ms932",
        "ms_kanji",
        "shift-jis",
        "shift_jis",
        "sjis",
        "windows-31j",
        "x-sjis",
    ),
    "euc-kr": (
        "cseuckr",
        "csksc56011987",
        "euc-kr",
        "iso-ir-149",
        "korean",
        "ks_c_5601-1987",
        "ks_c_5601-1989",
        "ksc5601",
        "ksc_5601",
        "windows-949",
    ),
    "replacement": (
        "csiso2022kr",
        "hz-gb-2312",
        "iso-2022-cn",
        "iso-2022-cn-ext",
        "iso-2022-kr",
        "replacement",
    ),
    "utf-16be": ("unicodefffe", "utf-16be"),
    "utf-16le": (
        "csunicode",
        "iso-10646-ucs-2",
        "ucs-2",
        "unicode",
        "unicodefeff",
        "utf-16",
        "utf-16le",
    ),
    "x-user-defined": ("x-user-defined",),
}

_LABELS: dict[str, str] = {
    label: enc for enc, labels in _LABELS_BY_ENCODING.items() for label in labels
}

# -- WHATWG encoding -> how Python decodes it --------------------------------

#: Multi-byte encodings: the Python codec whose decoder matches the WHATWG one.
#: WHATWG's decoders are supersets of the legacy standards their labels name.
_MULTI_BYTE = {
    "utf-8": "utf-8",
    "big5": "big5hkscs",
    "euc-jp": "euc_jp",
    "iso-2022-jp": "iso2022_jp_ext",
    "shift_jis": "cp932",
    "euc-kr": "cp949",
    "utf-16be": "utf-16-be",
    "utf-16le": "utf-16-le",
}

#: Single-byte encodings: the Python codec to start the 256-entry decode table
#: from, before the WHATWG corrections below.
_SINGLE_BYTE = {
    "ibm866": "cp866",
    "iso-8859-2": "iso8859_2",
    "iso-8859-3": "iso8859_3",
    "iso-8859-4": "iso8859_4",
    "iso-8859-5": "iso8859_5",
    "iso-8859-6": "iso8859_6",
    "iso-8859-7": "iso8859_7",
    "iso-8859-8": "iso8859_8",
    "iso-8859-8-i": "iso8859_8",
    "iso-8859-10": "iso8859_10",
    "iso-8859-13": "iso8859_13",
    "iso-8859-14": "iso8859_14",
    "iso-8859-15": "iso8859_15",
    "iso-8859-16": "iso8859_16",
    "koi8-r": "koi8_r",
    "koi8-u": "koi8_u",
    "macintosh": "mac_roman",
    "windows-874": "cp874",
    "windows-1250": "cp1250",
    "windows-1251": "cp1251",
    "windows-1252": "cp1252",
    "windows-1253": "cp1253",
    "windows-1254": "cp1254",
    "windows-1255": "cp1255",
    "windows-1256": "cp1256",
    "windows-1257": "cp1257",
    "windows-1258": "cp1258",
    "x-mac-cyrillic": "mac_cyrillic",
}

#: Bytes where the WHATWG index differs from the Python codec (beyond the
#: windows-* C1 rule applied in ``_single_byte_table``).
_OVERRIDES = {
    "koi8-u": {0xAE: "ў", 0xBE: "Ў"},  # WHATWG KOI8-U is KOI8-RU
    "windows-1255": {0xCA: "ֺ"},
}

_UNMAPPED = "\ufffd"  # a byte the WHATWG index leaves unmapped
_TABLES: dict[str, str] = {}


def _single_byte_table(encoding: str) -> str:
    table = _TABLES.get(encoding)
    if table is None:
        codec = _SINGLE_BYTE[encoding]
        chars = []
        for b in range(256):
            try:
                chars.append(bytes([b]).decode(codec))
            except UnicodeDecodeError:
                # A windows-* hole in 0x80-0x9F decodes to the C1 control of the
                # same value in WHATWG (and browsers); cp1252 leaves it unmapped.
                chr_c1 = chr(b) if encoding.startswith("windows-") and b < 0xA0 else _UNMAPPED
                chars.append(chr_c1)
        for b, ch in _OVERRIDES.get(encoding, {}).items():
            chars[b] = ch
        table = _TABLES[encoding] = "".join(chars)
    return table


#: x-user-defined: ASCII, then bytes 0x80-0xFF map to U+F780-U+F7FF.
_X_USER_DEFINED = "".join(chr(b) for b in range(0x80)) + "".join(
    chr(0xF780 + b) for b in range(0x80)
)


# -- gb18030 (and GBK, which WHATWG decodes with the same decoder) -----------
#
# Python's gb18030 codec agrees with the WHATWG decoder on every well-formed
# sequence except the 21 below (GB18030-2022 moved them off the Private Use
# Area), but not on the single byte 0x80 (WHATWG: the euro sign) nor on how it
# recovers from a broken sequence. So the bytes are split here, per the WHATWG
# algorithm: runs of well-formed sequences go to the codec in one call, and
# everything else is decoded step by step.

_GB_OVERRIDES = {
    b"\xa3\xa0": "\u3000",
    b"\xa6\xd9": "\ufe10",
    b"\xa6\xda": "\ufe12",
    b"\xa6\xdb": "\ufe11",
    b"\xa6\xdc": "\ufe13",
    b"\xa6\xdd": "\ufe14",
    b"\xa6\xde": "\ufe15",
    b"\xa6\xdf": "\ufe16",
    b"\xa6\xec": "\ufe17",
    b"\xa6\xed": "\ufe18",
    b"\xa6\xf3": "\ufe19",
    b"\xa8\xbc": "\u1e3f",
    b"\xfe\x59": "\u9fb4",
    b"\xfe\x61": "\u9fb5",
    b"\xfe\x66": "\u9fb6",
    b"\xfe\x67": "\u9fb7",
    b"\xfe\x6d": "\u9fb8",
    b"\xfe\x7e": "\u9fb9",
    b"\xfe\x90": "\u9fba",
    b"\xfe\xa0": "\u9fbb",
    b"\x81\x35\xf4\x37": "\ue7c7",
}
#: A run of well-formed sequences that are not overrides: ASCII, a two-byte
#: sequence, or a four-byte sequence (the second byte tells them apart). The
#: repeat is possessive: a greedy one keeps a backtrack point per sequence,
#: over a hundred times the body's size in memory for a long ASCII run.
_GB_RUN = re.compile(
    rb"(?:[\x00-\x7f]"
    rb"|(?!\xa3\xa0|\xa6[\xd9-\xdf\xec\xed\xf3]|\xa8\xbc|\xfe[\x59\x61\x66\x67\x6d\x7e\x90\xa0])"
    rb"[\x81-\xfe][\x40-\x7e\x80-\xfe]"
    rb"|(?!\x81\x35\xf4\x37)[\x81-\xfe][\x30-\x39][\x81-\xfe][\x30-\x39])++"
)


def _gb_sequence(seq: bytes) -> str:
    """One well-formed sequence: an override, the codec's answer, or U+FFFD for
    a four-byte pointer WHATWG leaves unmapped."""
    if seq in _GB_OVERRIDES:
        return _GB_OVERRIDES[seq]
    try:
        return seq.decode("gb18030")
    except UnicodeDecodeError:
        return "\ufffd"


def _gb_step(data: bytes, i: int) -> tuple[str, int]:
    """Decode the sequence at ``data[i]`` the WHATWG way: ``(text, consumed)``.
    A byte that ends a broken sequence is not consumed when it can start the
    next one."""
    n = len(data)
    first = data[i]
    if first < 0x80:
        return chr(first), 1
    if first == 0x80:
        return "\u20ac", 1
    if first == 0xFF or i + 1 == n:
        return "\ufffd", 1
    second = data[i + 1]
    if 0x30 <= second <= 0x39:
        if i + 2 == n:
            return "\ufffd", 2
        if not 0x81 <= data[i + 2] <= 0xFE:
            return "\ufffd", 1
        if i + 3 == n:
            return "\ufffd", 3
        if not 0x30 <= data[i + 3] <= 0x39:
            return "\ufffd", 1
        return _gb_sequence(data[i : i + 4]), 4
    if 0x40 <= second <= 0x7E or 0x80 <= second <= 0xFE:
        return _gb_sequence(data[i : i + 2]), 2
    return "\ufffd", 1 if second < 0x80 else 2


def _decode_gb18030(data: bytes) -> str:
    out: list[str] = []
    i, n = 0, len(data)
    while i < n:
        run = _GB_RUN.match(data, i)
        if run:
            end = run.end()
            try:
                out.append(data[i:end].decode("gb18030"))
                i = end
                continue
            except UnicodeDecodeError:  # an unmapped four-byte pointer inside
                while i < end:
                    text, used = _gb_step(data, i)
                    out.append(text)
                    i += used
                continue
        text, used = _gb_step(data, i)
        out.append(text)
        i += used
    return "".join(out)


def _decode_as(data: bytes, encoding: str) -> str:
    if encoding in ("gbk", "gb18030"):
        return _decode_gb18030(data)
    if encoding in _MULTI_BYTE:
        return data.decode(_MULTI_BYTE[encoding], errors="replace")
    if encoding == "x-user-defined":
        table = _X_USER_DEFINED
    else:
        table = _single_byte_table(encoding)
    # Every code point of a latin-1 decode is below 256: an index into the table.
    return data.decode("latin-1").translate(table)


def lookup(label: str) -> str | None:
    """Resolve a label to a WHATWG encoding name, or ``None`` when it is unknown
    or maps to "replacement" (both are ignored by the ladder)."""
    if not label.isascii():  # ASCII case-insensitive only: "\u212aoi8-r" is not a label
        return None
    enc = _LABELS.get(label.strip("\t\n\f\r ").lower())
    return None if enc in (None, "replacement") else enc


# -- the ladder ---------------------------------------------------------------


_HTTP_WS = "\t\n\r "


def _quoted_string(s: str, pos: int) -> tuple[str, int]:
    """WHATWG "collect an HTTP quoted string" (extract value) at ``s[pos] == '"'``;
    an unterminated string runs to the end."""
    out: list[str] = []
    pos += 1
    while pos < len(s):
        ch = s[pos]
        pos += 1
        if ch == '"':
            break
        if ch == "\\":
            if pos >= len(s):
                out.append("\\")
                break
            ch = s[pos]
            pos += 1
        out.append(ch)
    return "".join(out), pos


def _mime_parameters(s: str) -> dict[str, str]:
    """The parameters after a MIME type's first ``;``, parsed as the WHATWG
    MIME Sniffing Standard does: names are lower-cased, a quoted value is
    unquoted, and the FIRST occurrence of a name wins."""
    params: dict[str, str] = {}
    pos, n = 0, len(s)
    while pos < n:
        while pos < n and s[pos] in _HTTP_WS:
            pos += 1
        start = pos
        while pos < n and s[pos] not in ";=":
            pos += 1
        name = s[start:pos].lower()
        if pos < n and s[pos] == ";":
            pos += 1
            continue
        pos += 1  # past "="
        if pos >= n:
            break
        if s[pos] == '"':
            value, pos = _quoted_string(s, pos)
            while pos < n and s[pos] != ";":
                pos += 1
        else:
            start = pos
            while pos < n and s[pos] != ";":
                pos += 1
            value = s[start:pos].rstrip(_HTTP_WS)
        pos += 1  # past ";"
        if name and value and name not in params:
            params[name] = value
    return params


def _parse_content_type(content_type: str | None) -> tuple[str, str | None]:
    """Return ``(essence, charset)`` from a Content-Type value."""
    if not content_type:
        return "", None
    essence, _, rest = content_type.strip(_HTTP_WS).partition(";")
    return essence.strip(_HTTP_WS).lower(), _mime_parameters(rest).get("charset")


def _is_json(essence: str) -> bool:
    return essence.endswith("/json") or essence.endswith("+json")


# -- the HTML <meta> prescan (HTML Standard, "prescan a byte stream to
# determine its encoding"), limited to <meta>: comments and the attributes of
# other tags are skipped, and running out of bytes inside a construct ends the
# prescan with no answer. --------------------------------------------------------

_SPACE = b"\t\n\x0c\r "
_FAILED = "failed"  # a label that names no usable encoding (distinct from "none yet")


class _OutOfBytes(Exception):
    pass


def _at(data: bytes, pos: int) -> int:
    if pos >= len(data):
        raise _OutOfBytes
    return data[pos]


def _lower(byte: int) -> str:
    return chr(byte + 0x20 if 0x41 <= byte <= 0x5A else byte)


def _attribute(data: bytes, pos: int) -> tuple[tuple[str, str] | None, int]:
    """ "Get an attribute": ``((name, value) | None, position)``."""
    while _at(data, pos) in _SPACE or data[pos] == 0x2F:
        pos += 1
    if data[pos] == 0x3E:
        return None, pos
    name: list[str] = []
    value: list[str] = []
    while True:
        byte = _at(data, pos)
        if byte == 0x3D and name:
            pos += 1
            break
        if byte in _SPACE:
            while _at(data, pos) in _SPACE:
                pos += 1
            if data[pos] != 0x3D:
                return ("".join(name), ""), pos
            pos += 1
            break
        if byte in (0x2F, 0x3E):
            return ("".join(name), ""), pos
        name.append(_lower(byte))
        pos += 1
    while _at(data, pos) in _SPACE:
        pos += 1
    byte = data[pos]
    if byte in (0x22, 0x27):
        while True:
            pos += 1
            inner = _at(data, pos)
            if inner == byte:
                return ("".join(name), "".join(value)), pos + 1
            value.append(_lower(inner))
    if byte == 0x3E:
        return ("".join(name), ""), pos
    while True:
        value.append(_lower(byte))
        pos += 1
        byte = _at(data, pos)
        if byte in _SPACE or byte == 0x3E:
            return ("".join(name), "".join(value)), pos


def _label(text: str) -> str:
    return lookup(text) or _FAILED


def _charset_from_content(content: str) -> str | None:
    """ "Extract a character encoding from a meta element" (the ``content``
    attribute of an http-equiv meta)."""
    pos, n = 0, len(content)
    while True:
        found = content.find("charset", pos)
        if found < 0:
            return None
        pos = found + 7
        while pos < n and content[pos] in _HTTP_WS + "\x0c":
            pos += 1
        if pos >= n or content[pos] != "=":
            continue
        pos += 1
        while pos < n and content[pos] in _HTTP_WS + "\x0c":
            pos += 1
        if pos >= n:
            return None
        quote = content[pos]
        if quote in "\"'":
            end = content.find(quote, pos + 1)
            return None if end < 0 else _label(content[pos + 1 : end])
        end = pos
        while end < n and content[end] not in _HTTP_WS + "\x0c;":
            end += 1
        return _label(content[pos:end])


def _meta(data: bytes, pos: int) -> tuple[str | None, int]:
    """The attributes of one ``<meta>``; ``(encoding | None, position)``."""
    seen: set[str] = set()
    got_pragma = False
    need_pragma: bool | None = None
    charset: str | None = None
    while True:
        attribute, pos = _attribute(data, pos)
        if attribute is None:
            break
        name, value = attribute
        if name in seen:
            continue
        seen.add(name)
        if name == "http-equiv":
            got_pragma = got_pragma or value == "content-type"
        elif name == "content":
            found = _charset_from_content(value)
            if found not in (None, _FAILED) and charset is None:
                charset, need_pragma = found, True
        elif name == "charset":
            charset, need_pragma = _label(value), False
    if need_pragma is None or (need_pragma and not got_pragma) or charset in (None, _FAILED):
        return None, pos
    return charset, pos


def _prescan(data: bytes) -> str | None:
    """The encoding a ``<meta>`` in ``data`` declares, or ``None``."""
    pos, n = 0, len(data)
    try:
        while pos < n:
            if data.startswith(b"<!--", pos):
                end = data.find(b"-->", pos + 2)
                if end < 0:
                    return None
                pos = end + 2
            elif data[pos : pos + 5].lower() == b"<meta" and _at(data, pos + 5) in _SPACE + b"/":
                found, pos = _meta(data, pos + 5)
                if found is not None:
                    return found
            elif data[pos] == 0x3C and _is_tag(data, pos):
                while _at(data, pos) not in _SPACE + b">":
                    pos += 1
                attribute: tuple[str, str] | None = ("", "")
                while attribute is not None:
                    attribute, pos = _attribute(data, pos)
            elif data.startswith((b"<!", b"</", b"<?"), pos):
                end = data.find(b">", pos + 1)
                if end < 0:
                    return None
                pos = end
            pos += 1
    except _OutOfBytes:
        return None
    return None


def _is_tag(data: bytes, pos: int) -> bool:
    """``<`` or ``</`` followed by an ASCII letter."""
    pos += 2 if data[pos + 1 : pos + 2] == b"/" else 1
    return pos < len(data) and (0x41 <= data[pos] <= 0x5A or 0x61 <= data[pos] <= 0x7A)


_META_SNIFF_BYTES = 1024
_HTML_TYPES = ("", "text/html", "application/xhtml+xml")


def decode(body: bytes, content_type: str | None) -> str:
    """Decode a response body per the WHATWG ladder."""
    if body.startswith(b"\xef\xbb\xbf"):
        return _decode_as(body[3:], "utf-8")
    if body.startswith(b"\xfe\xff"):
        return _decode_as(body[2:], "utf-16be")
    if body.startswith(b"\xff\xfe"):
        return _decode_as(body[2:], "utf-16le")

    essence, charset = _parse_content_type(content_type)
    if charset:
        enc = lookup(charset)
        if enc is not None:
            return _decode_as(body, enc)
    if _is_json(essence):
        return _decode_as(body, "utf-8")
    if essence in _HTML_TYPES:
        enc = _prescan(body[:_META_SNIFF_BYTES])
        if enc is not None:
            # HTML: a <meta> cannot switch to UTF-16 (the bytes it was read
            # from were ASCII-compatible), and x-user-defined means 1252.
            if enc in ("utf-16be", "utf-16le"):
                enc = "utf-8"
            elif enc == "x-user-defined":
                enc = "windows-1252"
            return _decode_as(body, enc)
    return _decode_as(body, "utf-8")


__all__ = ["decode", "lookup"]
