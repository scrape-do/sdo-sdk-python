# SPDX-License-Identifier: Apache-2.0
"""Bounded, incremental ``Content-Encoding`` decoding.

The scraped target controls the response body, so a few kilobytes of gzip or
brotli can inflate to gigabytes. httpx's own decoders inflate each network
chunk in one unbounded call (brotli without an output limit), so the SDK reads
the raw bytes and decodes them here: every decoder call is capped at ``_STEP``
bytes of output and the running total is checked after each call. Memory
therefore never exceeds the ceiling plus one step, whatever the ratio.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator

import brotli

#: Output produced per decoder call.
_STEP = 64 * 1024
#: The most codings one body is decoded through (urllib3's limit too). Each
#: stage holds its own decoder state and buffers, and the target controls the
#: header, so a longer chain is a :class:`DecodeError`, whatever the body.
MAX_CODINGS = 5
_GZIP_MAGIC = b"\x1f\x8b"


class DecodeError(Exception):
    """The body is not valid for its declared ``Content-Encoding``."""


class TooLarge(Exception):
    """A decoding stage produced more than the ceiling."""


class _Zlib:
    """gzip (including concatenated members) and deflate (zlib-wrapped or raw,
    since servers send both)."""

    def __init__(self, coding: str) -> None:
        self._gzip = coding == "gzip"
        self._d = zlib.decompressobj(16 + zlib.MAX_WBITS) if self._gzip else None
        self._carry = b""  # input held back until there is enough to decide on
        self._fed = False

    def feed(self, data: bytes, final: bool) -> Iterator[bytes]:
        data = self._carry + data
        self._carry = b""
        self._fed = self._fed or bool(data)
        yield from self._inflate(data, final)
        if final and self._fed and (self._d is None or not self._d.eof):
            # A stream cut short is an error, not a shorter body: the framing
            # said the body was complete, so the payload itself is damaged.
            raise zlib.error("the compressed stream ended before its end marker")

    def _inflate(self, data: bytes, final: bool) -> Iterator[bytes]:
        if self._d is None:
            # Two bytes tell zlib-wrapped deflate (CM=8, header a multiple of
            # 31) from raw deflate.
            if len(data) < 2 and not final:
                self._carry = data
                return
            wrapped = len(data) >= 2 and data[0] & 0x0F == 8 and (data[0] << 8 | data[1]) % 31 == 0
            self._d = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
        d = self._d
        while data:
            if d.eof:
                # After the end of the stream only another gzip member is
                # decoded; anything else is ignored, as httpx and browsers do.
                if not self._gzip:
                    return
                if len(data) < 2 and not final:
                    self._carry = data
                    return
                if data[:2] != _GZIP_MAGIC:
                    return
                d = self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out = d.decompress(data, _STEP)
            yield out
            if d.eof:
                data = d.unused_data
                continue
            data = d.unconsumed_tail
            # Output capped with every input byte consumed: zlib may still hold
            # output (e.g. a long back-reference), so drain it in steps too.
            while not data and len(out) == _STEP and not d.eof:
                out = d.decompress(b"", _STEP)
                yield out


def _drain(d: brotli.Decompressor, data: bytes) -> Iterator[bytes]:
    """Feed ``data`` and yield the output in ``_STEP``-sized pieces."""
    yield d.process(data, output_buffer_limit=_STEP)
    # The limit stops the output buffer from growing; what did not fit stays
    # inside the decoder, so drain it with empty input before the next chunk
    # (can_accept_more_data() alone does not signal it).
    while not d.is_finished():
        out = d.process(b"", output_buffer_limit=_STEP)
        if not out and d.can_accept_more_data():
            break
        yield out


class _Brotli:
    """brotli. Bytes after the end of a complete stream are ignored, as for
    gzip. The decoder rejects them when they arrive in the same call as the
    end, and cannot be asked where the stream ended, so that case is replayed
    from the input kept so far (see :meth:`_replay`)."""

    def __init__(self) -> None:
        self._d = brotli.Decompressor()
        self._seen: list[bytes] = []  # input so far, until the stream ends
        self._emitted = 0  # output yielded so far
        self._done = False

    def feed(self, data: bytes, final: bool) -> Iterator[bytes]:
        if self._done:
            return  # data after the end of a complete stream is ignored
        if final and self._seen and not self._d.is_finished():
            raise brotli.error("the brotli stream ended before its end marker")
        if not data:
            return
        self._seen.append(data)
        try:
            for out in _drain(self._d, data):
                self._emitted += len(out)
                yield out
        except brotli.error:
            yield from self._replay()
        if self._d.is_finished():
            self._done = True
            self._seen = []

    @staticmethod
    def _state(data: bytes) -> str:
        """``"open"`` when ``data`` is an unfinished prefix of a valid stream,
        else ``"closed"`` (it ended or is corrupt). Monotonic in the prefix
        length, so the end can be bisected. Bounded: the decoder only rejects
        trailing bytes after producing all its output, which the caller had
        already counted against the cap, and a prefix produces no more."""
        d = brotli.Decompressor()
        try:
            for _ in _drain(d, data):
                pass
        except brotli.error:
            return "closed"
        return "closed" if d.is_finished() else "open"

    def _replay(self) -> Iterator[bytes]:
        """The last chunk failed: find the shortest prefix of it after which
        the stream is closed, decode up to there with a fresh decoder (output
        already yielded is skipped), and ignore the rest if the stream ended
        there. A stream that is corrupt instead raises again."""
        head, chunk = b"".join(self._seen[:-1]), self._seen[-1]
        lo, hi = 0, len(chunk)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._state(head + chunk[:mid]) == "open":
                lo = mid + 1
            else:
                hi = mid
        d = brotli.Decompressor()
        skip = self._emitted
        for out in _drain(d, head + chunk[:lo]):
            if skip >= len(out):
                skip -= len(out)
                continue
            out, skip = out[skip:], 0
            self._emitted += len(out)
            yield out
        # Here the stream ended at ``lo``: a corrupt one raised in _drain.
        self._d = d


_ALIASES = {"x-gzip": "gzip"}


def codings(header: str | None) -> list[str]:
    """The codings to undo, in undo order. The header lists them in the order
    they were applied, so they are undone from the last one; ``identity`` is
    skipped, and an unsupported coding stops decoding there (the bytes are then
    returned as they are at that point)."""
    out = []
    for token in reversed((header or "").split(",")):
        coding = token.strip().lower()
        coding = _ALIASES.get(coding, coding)
        if coding in ("", "identity"):
            continue
        if coding not in ("gzip", "deflate", "br"):
            break
        out.append(coding)
    return out


class BodyDecoder:
    """Feed raw chunks, get the decoded body. Raises :class:`TooLarge` as soon
    as any stage's output crosses ``limit``, and :class:`DecodeError` on a
    corrupt stream or a chain of more than :data:`MAX_CODINGS` codings."""

    def __init__(self, content_encoding: str | None, limit: int) -> None:
        self._limit = limit
        chain = codings(content_encoding)
        # Raised on the first feed or finish, where the caller handles it.
        self._error: str | None = None
        if len(chain) > MAX_CODINGS:
            self._error, chain = f"more than {MAX_CODINGS} content codings", []
        self._stages = [_Brotli() if c == "br" else _Zlib(c) for c in chain]
        self._totals = [0] * (len(self._stages) + 1)
        self._parts: list[bytes] = []

    def _fit(self, index: int, piece: bytes) -> bytes:
        """``piece`` as far as it fits under the cap for stage ``index``; the
        caller raises :class:`TooLarge` when it was cut, after passing the
        prefix on, so a capped read still holds everything up to the cap."""
        room = self._limit - self._totals[index]
        piece = piece[:room] if len(piece) > room else piece
        self._totals[index] += len(piece)
        return piece

    def _run(self, index: int, data: bytes, final: bool) -> None:
        if self._error:
            raise DecodeError(self._error)
        if index == len(self._stages):
            kept = self._fit(index, data)
            self._parts.append(kept)
            if len(kept) < len(data):
                raise TooLarge
            return
        try:
            for piece in self._stages[index].feed(data, final):
                kept = self._fit(index, piece)
                if kept:
                    self._run(index + 1, kept, False)
                if len(kept) < len(piece):
                    raise TooLarge
        except (zlib.error, brotli.error) as exc:
            raise DecodeError(str(exc)) from None
        if final:
            self._run(index + 1, b"", True)

    def feed(self, chunk: bytes) -> None:
        self._run(0, chunk, False)

    def finish(self) -> bytes:
        self._run(0, b"", True)
        return self.partial()

    def partial(self) -> bytes:
        """The decoded bytes so far (at most ``limit``)."""
        return b"".join(self._parts)


__all__ = ["BodyDecoder", "DecodeError", "TooLarge", "codings"]
