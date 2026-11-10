"""
Negotiated response compression, off the event loop.

Verified Starlette/uvicorn behavior this module relies on:
- StreamingResponse iterates a *sync* generator via iterate_in_threadpool —
  every chunk is compressed in the AnyIO threadpool, never in the event loop.
- GZipMiddleware passes through untouched any response whose headers already
  carry Content-Encoding — so every branch here sets it, including an explicit
  "identity".
- uvicorn h11 applies write flow control on send(): a slow client parks the
  compressor thread; memory stays ~one chunk.
- GIL probe (see plan): brotli/zstandard/zlib all release the GIL while
  compressing a 256 KB chunk (measured loop lag ≤ 0.31 ms).
"""
from __future__ import annotations

import zlib
from time import perf_counter
from typing import Iterator, Optional

from fastapi.responses import Response, StreamingResponse

from app.services import perflog

try:
    import brotli
except ImportError:  # pragma: no cover
    brotli = None
try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

CHUNK_SIZE = 256 * 1024      # per-chunk compress unit (one threadpool hop each)
SMALL_BODY = 64 * 1024       # <= this: one-shot compress inside the sync route
PROBE_BYTES = 64 * 1024      # incompressibility probe window
PROBE_MIN_RATIO = 0.95       # probe output >= 95% of input -> serve identity
MIN_COMPRESS = 1024          # below this: identity (parity with gzip minimum_size)
BR_QUALITY = 5
ZSTD_LEVEL = 6
GZIP_LEVEL = 6


def accepted_encodings(accept_encoding: str) -> set[str]:
    """Codings the client accepts (q > 0), with '*' expanded to the codecs we
    ship. An explicit "coding;q=0" always wins over '*' (RFC 9110)."""
    accepted: set[str] = set()
    refused: set[str] = set()
    for part in (accept_encoding or "").lower().split(","):
        token, _, params = part.partition(";")
        token = token.strip()
        if not token:
            continue
        q = 1.0
        for param in params.split(";"):  # q may not be the first param (e.g. "gzip;level=1;q=0")
            name, _, value = param.partition("=")
            if name.strip() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0  # malformed q -> don't send a coding the client may have refused
                break
        if q <= 0.0:
            refused.add(token)
        else:
            accepted.add(token)
    if "*" in accepted:
        accepted |= {"br", "zstd", "gzip"} - refused
    return accepted


# negotiate() is a pure function of the header string and browsers send the
# same handful of Accept-Encoding values forever — memoize (bounded; the GIL
# makes plain dict ops safe). Saves the parse (~1-2 µs) on every warm hit.
_NEG_CACHE: dict[str, str] = {}


def negotiate(accept_encoding: str) -> str:
    """Pick the best encoding the client accepts: br > zstd > gzip > identity."""
    enc = _NEG_CACHE.get(accept_encoding)
    if enc is not None:
        return enc
    accepted = accepted_encodings(accept_encoding)
    if brotli is not None and "br" in accepted:
        enc = "br"
    elif zstandard is not None and "zstd" in accepted:
        enc = "zstd"
    elif "gzip" in accepted:
        enc = "gzip"
    else:
        enc = "identity"
    if len(accept_encoding) < 256:  # don't let hostile headers grow the memo
        if len(_NEG_CACHE) > 256:
            _NEG_CACHE.clear()
        _NEG_CACHE[accept_encoding] = enc
    return enc


def is_compressible(body: bytes) -> bool:
    if len(body) < MIN_COMPRESS:
        return False
    # Sample the middle: the head of a rendered dashboard is injected JS
    # (always compressible) and would mask an incompressible bulk.
    mid = max(0, (len(body) - PROBE_BYTES) // 2)
    probe = body[mid:mid + PROBE_BYTES]
    return len(zlib.compress(probe, 1)) < PROBE_MIN_RATIO * len(probe)


class _Compressor:
    """Uniform incremental interface over the three codecs."""

    def __init__(self, enc: str, level: Optional[int] = None):
        self.enc = enc
        if enc == "br":
            self._c = brotli.Compressor(quality=BR_QUALITY if level is None else level)
            self.compress, self.finish = self._c.process, self._c.finish
        elif enc == "zstd":
            self._c = zstandard.ZstdCompressor(
                level=ZSTD_LEVEL if level is None else level).compressobj()
            self.compress, self.finish = self._c.compress, self._c.flush
        else:  # gzip
            self._c = zlib.compressobj(GZIP_LEVEL if level is None else level,
                                       wbits=31)  # wbits=31 = gzip container
            self.compress, self.finish = self._c.compress, self._c.flush


def _record_stages(rec: Optional[dict], enc: str, n_in: int, n_out: int, ms: float) -> None:
    if rec is None:
        return
    stages = rec.setdefault("stages", {})
    stages["enc_" + enc] = 1
    stages["enc_in"] = n_in
    stages["enc_out"] = n_out
    stages["enc_ratio"] = round(n_out / n_in, 4) if n_in else 0.0
    stages["enc_ms"] = round(ms, 3)


def encode_bytes(body: bytes, enc: str, level: Optional[int] = None) -> bytes:
    """One-shot full compression (cache fill, warm-up). level overrides the
    request-path default — background warm-up can afford a stronger setting."""
    comp = _Compressor(enc, level)
    return comp.compress(body) + comp.finish()


# A pre-compressed slot is attacker-supplied (any can_update_data grantee) and
# this decode runs for any client that can't take its coding — including an
# anonymous share-link visitor. Without a ceiling a kilobyte of crafted gzip
# expands to gigabytes and takes the container with it. No real artifact comes
# near this: the largest slot measured in production is 43 MB compressed.
_MAX_DECODE_BYTES = 256 * 1024 * 1024
_DECODE_CHUNK = 1 << 16


class DecodedTooLarge(ValueError):
    """A pre-compressed artifact expands past _MAX_DECODE_BYTES."""


def decode_bytes(payload: bytes, enc: str, max_out: int = _MAX_DECODE_BYTES) -> bytes:
    """Inverse of encode_bytes — lets a pre-compressed artifact be served as
    identity to a client that can't decode its coding.

    Bounded: output is accumulated incrementally and abandoned the moment it
    passes max_out, so a decompression bomb costs a chunk, not the host.
    """
    if enc == "br":
        dec = brotli.Decompressor()
        out, total = [], 0
        for i in range(0, len(payload), _DECODE_CHUNK):
            part = dec.process(payload[i:i + _DECODE_CHUNK])
            total += len(part)
            if total > max_out:
                raise DecodedTooLarge(f"br output exceeds {max_out} bytes")
            out.append(part)
        return b"".join(out)

    if enc == "zstd":
        # decompressobj: one-shot decompress() requires a content size in the
        # frame header, which streaming compressors don't write.
        reader = zstandard.ZstdDecompressor().stream_reader(payload)
        data = reader.read(max_out + 1)
        if len(data) > max_out:
            raise DecodedTooLarge(f"zstd output exceeds {max_out} bytes")
        return data

    # gzip or zlib container
    dec = zlib.decompressobj(wbits=47)
    data = dec.decompress(payload, max_out + 1)
    if len(data) > max_out or dec.unconsumed_tail:
        raise DecodedTooLarge(f"gzip output exceeds {max_out} bytes")
    return data


def _stream(body: bytes, enc: str, rec: Optional[dict], on_complete=None) -> Iterator[bytes]:
    """Sync generator: compression happens at iteration time — i.e. in the
    threadpool via Starlette's iterate_in_threadpool — chunk by chunk.
    on_complete(payload) fires only when the stream FINISHES (client aborts
    raise GeneratorExit before finish → a partial payload is never reported).
    enc_ms sums compress calls only — the generator is suspended between chunks
    by uvicorn's write flow control, and that client-transfer wait is not
    compression cost (hdr_ms/dur_ms already tell the transfer story)."""
    comp = _Compressor(enc)
    chunks: list[bytes] = []
    out_total = 0
    cpu_s = 0.0
    for i in range(0, len(body), CHUNK_SIZE):
        t0 = perf_counter()
        out = comp.compress(body[i:i + CHUNK_SIZE])
        cpu_s += perf_counter() - t0
        if out:
            out_total += len(out)
            if on_complete is not None:
                chunks.append(out)
            yield out
    t0 = perf_counter()
    tail = comp.finish()
    cpu_s += perf_counter() - t0
    if tail:
        out_total += len(tail)
        if on_complete is not None:
            chunks.append(tail)
        yield tail
    _record_stages(rec, enc, len(body), out_total, cpu_s * 1000)
    if on_complete is not None:
        on_complete(b"".join(chunks))


def enc_headers(enc: str, headers: Optional[dict] = None) -> dict:
    hdrs = dict(headers or {})
    hdrs["Content-Encoding"] = enc  # explicit identity too: GZipMiddleware must skip
    hdrs["Vary"] = "Accept-Encoding"
    return hdrs


def encoded_response(
    body: bytes,
    accept_encoding: str,
    *,
    media_type: str,
    headers: Optional[dict] = None,
    on_complete=None,
    enc: Optional[str] = None,
    compressible: Optional[bool] = None,
) -> Response:
    """Build the response for a (potentially huge) in-memory body.

    identity           -> plain Response, zero CPU, Content-Length set
    small (<= 64 KB)   -> one-shot compress here (caller is a sync/threadpool
                          route), Content-Length set
    large              -> chunked StreamingResponse, per-chunk compression in
                          the threadpool, headers flushed before any CPU work

    on_complete(enc, payload) is called with the full compressed payload once
    it is completely produced (immediately for one-shot; at successful stream
    end for large bodies) — used by the render cache to store the variant.

    enc / compressible: pass when already known (the render route negotiates
    once and the cache cell stores the probe verdict) to skip the redundant
    Accept-Encoding parse and 64 KB zlib probe.
    """
    rec = perflog.request_record.get()
    if enc is None:
        enc = negotiate(accept_encoding)
    if enc != "identity":
        if compressible is None:
            compressible = is_compressible(body)
        if not compressible:
            enc = "identity"

    hdrs = enc_headers(enc, headers)

    if enc == "identity":
        _record_stages(rec, enc, len(body), len(body), 0.0)
        return Response(content=body, media_type=media_type, headers=hdrs)

    if len(body) <= SMALL_BODY:
        t0 = perf_counter()
        payload = encode_bytes(body, enc)
        _record_stages(rec, enc, len(body), len(payload), (perf_counter() - t0) * 1000)
        if on_complete is not None:
            on_complete(enc, payload)
        return Response(content=payload, media_type=media_type, headers=hdrs)

    sink = (lambda payload: on_complete(enc, payload)) if on_complete is not None else None
    return StreamingResponse(_stream(body, enc, rec, sink), media_type=media_type, headers=hdrs)
