"""Screenshot validation: the type comes from the content, never from a name or declared MIME."""

import struct
import zlib

import pytest

from bot.services.image_check import MAX_BYTES, ImageError, inspect
from tests import payment_images as img


def _png(width=1, height=1, idat=None, *, crc_ok=True, trailing=b"", iend=True, depth=8, color=2):
    def chunk(kind, body, good=True):
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc if good else 0)

    row = b"\x00" + b"\x00" * (width * 3)
    data = zlib.compress(row * min(height, 50)) if idat is None else idat
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
    out += chunk(b"IDAT", data, crc_ok)
    if iend:
        out += chunk(b"IEND", b"")
    return out + trailing


@pytest.mark.parametrize(
    "data,kind",
    [
        (img.JPEG, "image/jpeg"),
        (img.PNG, "image/png"),
        (img.WEBP_LOSSY, "image/webp"),
        (img.WEBP_LOSSLESS, "image/webp"),
        (_png(20, 20), "image/png"),
    ],
)
def test_real_images_are_accepted_and_typed_from_their_content(data, kind):
    info = inspect(data)
    assert info.content_type == kind and info.width >= 1 and info.height >= 1


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello" * 20,
        b"<?php system($_GET['c']); ?>" * 5,
        b"<html><script>alert(1)</script></html>" * 3,
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' * 2,
        b"GIF89a" + b"\x01\x00\x01\x00" * 20,
        b"%PDF-1.7\n" + b"x" * 100,
        b"MZ" + b"\x90\x00" * 50,
        b"#!/bin/sh\nrm -rf /\n" * 4,
        bytes(range(256)),
    ],
)
def test_anything_that_is_not_jpeg_png_webp_is_refused(data):
    with pytest.raises(ImageError) as exc:
        inspect(data)
    assert exc.value.code == "not_image"


def test_an_extension_or_mime_claim_cannot_change_the_verdict():
    # inspect() only ever receives bytes: a php file named "shot.png" is still not an image
    with pytest.raises(ImageError):
        inspect(b"<?php echo 1; ?>" * 10)


def test_oversized_upload_is_refused_before_anything_else():
    with pytest.raises(ImageError) as exc:
        inspect(img.JPEG + b"\x00" * MAX_BYTES)
    assert exc.value.code == "too_big"
    inspect(img.PNG)  # and the limit itself is not hit by normal files


@pytest.mark.parametrize(
    "data",
    [
        img.JPEG[:-40],  # truncated
        img.JPEG[:3] + b"\x00" * 100,  # magic only
        img.JPEG + b"<?php system($_GET['c']); ?>",  # payload appended after the image
        img.PNG[:-12],  # IEND missing
        img.PNG + b"<?php ?>",  # data after IEND
        img.PNG[:30] + b"\xff" + img.PNG[31:],  # a flipped byte -> CRC mismatch
        _png(crc_ok=False),
        _png(iend=False),
        _png(idat=b"not zlib at all"),
        _png(idat=zlib.compress(b"\x00\x00\x00")[:-3]),  # incomplete zlib stream
        _png(width=100_000, height=100_000),  # absurd size
        _png(width=0),
        _png(trailing=b"junk"),
        img.WEBP_LOSSY[:-10],
        img.WEBP_LOSSY + b"extra",
        img.WEBP_LOSSY[:12] + b"JUNK" + img.WEBP_LOSSY[16:],
    ],
)
def test_corrupt_or_tampered_images_are_refused(data):
    with pytest.raises(ImageError) as exc:
        inspect(data)
    assert exc.value.code == "corrupt"


def test_a_decompression_bomb_is_refused_without_inflating_it():
    bomb = zlib.compress(b"\x00" * 50_000_000, 9)  # a few KB that would inflate to 50 MB
    assert len(bomb) < 100_000
    with pytest.raises(ImageError) as exc:
        inspect(_png(width=1, height=1, idat=bomb))
    assert exc.value.code == "corrupt"
