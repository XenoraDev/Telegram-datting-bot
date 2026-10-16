"""Synthetic payment screenshots drawn with Pillow (test-only): real pixels for the OCR path."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont


def screenshot(
    lines: list[str], *, fmt: str = "PNG", size: tuple[int, int] = (900, 1000), font_px: int = 40
) -> bytes:
    """A white 'phone screen' with the given text lines in black, as PNG / JPEG / WEBP bytes."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=font_px)
    y = 40
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += font_px + 24
    out = io.BytesIO()
    img.save(out, format=fmt)
    return out.getvalue()


def photo(fmt: str = "JPEG") -> bytes:
    """A 'normal photo': smooth gradients and shapes, no text at all."""
    img = Image.new("RGB", (800, 600))
    px = img.load()
    for x in range(800):
        for y in range(600):
            px[x, y] = (x * 255 // 800, y * 255 // 600, (x + y) * 255 // 1400)
    draw = ImageDraw.Draw(img)
    draw.ellipse((200, 150, 600, 450), outline="white", width=12)
    out = io.BytesIO()
    img.save(out, format=fmt)
    return out.getvalue()


def payment_lines(
    amount: str = "99.00",
    upi: str = "shop@okbank",
    utr: str = "412345678901",
    when: str = "10 Oct 2026, 10:42 AM",
    status: str = "Payment Successful",
) -> list[str]:
    return [
        status,
        f"Rs. {amount}",
        f"Paid to {upi}",
        f"UPI Ref No: {utr}",
        when,
    ]
