"""Strict validation of an uploaded payment screenshot (P6). Pure functions, no I/O.

An upload is untrusted bytes. Nothing here trusts a file name, an extension or a client-declared
MIME type: the type is decided from the content itself, the structure is walked (JPEG segments,
PNG chunks with their CRCs and a bounded zlib check, WebP RIFF/VP8 headers) and anything odd is
refused: wrong magic, truncation, bad checksums, trailing data after the image (the polyglot
trick), absurd dimensions. Only jpeg / png / webp pass; SVG, GIF, HTML, scripts, PDFs do not.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

MAX_BYTES = 3 * 1024 * 1024  # also a CHECK constraint on payment_evidence
MIN_BYTES = 32
MAX_SIDE = 12_000
MAX_PIXELS = 60_000_000
_JPEG_START_OF_FRAME = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class ImageError(ValueError):
    """The upload is not an acceptable image. `code` is one of: too_big, not_image, corrupt."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImageInfo:
    content_type: str
    width: int
    height: int


def _dims_ok(width: int, height: int) -> None:
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE and width * height <= MAX_PIXELS):
        raise ImageError("corrupt")


def inspect(data: bytes) -> ImageInfo:
    """Validated type and size of the image, or ImageError."""
    if len(data) > MAX_BYTES:
        raise ImageError("too_big")
    if len(data) < MIN_BYTES:
        raise ImageError("not_image")
    if data.startswith(b"\xff\xd8\xff"):
        return _jpeg(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    raise ImageError("not_image")


# ------------------------------------------------------------------ JPEG
def _jpeg(data: bytes) -> ImageInfo:
    n = len(data)
    i = 2
    size: tuple[int, int] | None = None
    while True:
        if i + 4 > n:
            raise ImageError("corrupt")
        if data[i] != 0xFF:
            raise ImageError("corrupt")
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            raise ImageError("corrupt")
        marker = data[i]
        i += 1
        if marker == 0xD9 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            raise ImageError("corrupt")  # EOI / RST before any scan
        (length,) = struct.unpack(">H", data[i : i + 2])
        if length < 2 or i + length > n:
            raise ImageError("corrupt")
        segment = data[i + 2 : i + length]
        if marker in _JPEG_START_OF_FRAME:
            if len(segment) < 6 or segment[0] not in (8, 12):
                raise ImageError("corrupt")
            height, width = struct.unpack(">HH", segment[1:5])
            _dims_ok(width, height)
            size = (width, height)
        i += length
        if marker == 0xDA:  # start of scan: entropy data follows until EOI
            break
    if size is None:
        raise ImageError("corrupt")
    tail = data.rstrip(b"\x00")
    if not tail.endswith(b"\xff\xd9") or len(tail) <= i:
        raise ImageError("corrupt")  # truncated (or something appended after the image)
    return ImageInfo("image/jpeg", *size)


# ------------------------------------------------------------------ PNG
_PNG_COLOR_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _png(data: bytes) -> ImageInfo:
    n = len(data)
    i = 8
    header: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    seen_idat = False
    while True:
        if i + 12 > n:
            raise ImageError("corrupt")
        (length,) = struct.unpack(">I", data[i : i + 4])
        kind = data[i + 4 : i + 8]
        end = i + 8 + length
        if length > n or end + 4 > n:
            raise ImageError("corrupt")
        body = data[i + 8 : end]
        (crc,) = struct.unpack(">I", data[end : end + 4])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ImageError("corrupt")
        if header is None:
            if kind != b"IHDR" or length != 13:
                raise ImageError("corrupt")
            width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if (
                color not in _PNG_COLOR_DEPTHS
                or depth not in _PNG_COLOR_DEPTHS[color]
                or comp != 0
                or filt != 0
                or interlace not in (0, 1)
            ):
                raise ImageError("corrupt")
            _dims_ok(width, height)
            header = (width, height, depth, color, interlace)
        elif kind == b"IDAT":
            seen_idat = True
            idat += body
            if len(idat) > MAX_BYTES:
                raise ImageError("too_big")
        elif kind == b"IEND":
            if length != 0 or end + 4 != n:  # nothing may follow IEND
                raise ImageError("corrupt")
            break
        i = end + 4
    if not seen_idat:
        raise ImageError("corrupt")
    width, height, depth, color, _interlace = header
    _check_png_stream(bytes(idat), width, height, depth, color)
    return ImageInfo("image/png", width, height)


def _check_png_stream(idat: bytes, width: int, height: int, depth: int, color: int) -> None:
    """The zlib stream must be complete and decompress to a sane size (a bomb is refused)."""
    bits = depth * _PNG_CHANNELS[color]
    row = (width * bits + 7) // 8 + 1
    limit = row * height * 2 + 4096  # interlaced images are somewhat larger than the plain size
    d = zlib.decompressobj()
    total = 0
    try:
        chunk = d.decompress(idat, 1 << 20)
        total += len(chunk)
        while d.unconsumed_tail and total <= limit:
            chunk = d.decompress(d.unconsumed_tail, 1 << 20)
            total += len(chunk)
    except zlib.error as exc:
        raise ImageError("corrupt") from exc
    if total > limit or not d.eof or d.unused_data:
        raise ImageError("corrupt")


# ------------------------------------------------------------------ WebP
def _webp(data: bytes) -> ImageInfo:
    n = len(data)
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size + 8 != n and riff_size + 9 != n:  # RIFF pads odd sizes to even
        raise ImageError("corrupt")
    kind = data[12:16]
    (chunk_size,) = struct.unpack("<I", data[16:20])
    if 20 + chunk_size > n:
        raise ImageError("corrupt")
    body = data[20 : 20 + chunk_size]
    if kind == b"VP8 ":
        if len(body) < 10 or body[3:6] != b"\x9d\x01\x2a" or body[0] & 1:
            raise ImageError("corrupt")
        width, height = struct.unpack("<HH", body[6:10])
        width, height = width & 0x3FFF, height & 0x3FFF
    elif kind == b"VP8L":
        if len(body) < 5 or body[0] != 0x2F:
            raise ImageError("corrupt")
        bits = struct.unpack("<I", body[1:5])[0]
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8X":
        if len(body) < 10:
            raise ImageError("corrupt")
        width = int.from_bytes(body[4:7], "little") + 1
        height = int.from_bytes(body[7:10], "little") + 1
    else:
        raise ImageError("corrupt")
    _dims_ok(width, height)
    return ImageInfo("image/webp", width, height)
