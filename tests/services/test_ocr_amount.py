"""Real Tesseract on a drawn Paytm-style receipt: the big headline amount must come out right."""

import io
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from bot.services.evidence import OK, Expected, analyze_text
from bot.services.ocr import TesseractOcr

BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
PLAIN = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
pytestmark = pytest.mark.skipif(
    shutil.which("tesseract") is None or not BOLD.exists() or not PLAIN.exists(),
    reason="needs tesseract and the DejaVu fonts",
)

UPI = "BHARATPE.8F0I1R0D3A21265@fbpe"
NOW = datetime(2026, 10, 29, 10, 50, tzinfo=UTC)


def receipt(amount: str, big: int) -> bytes:
    img = Image.new("RGB", (1080, 1900), "white")
    d = ImageDraw.Draw(img)
    d.text((300, 200), "Payment Successful", font=ImageFont.truetype(str(BOLD), 56), fill="green")
    d.text((380, 330), f"₹{amount}", font=ImageFont.truetype(str(BOLD), big), fill="black")
    d.text((120, 560), "Paid to", font=ImageFont.truetype(str(PLAIN), 44), fill="gray")
    d.text((120, 630), UPI, font=ImageFont.truetype(str(BOLD), 44), fill="black")
    d.text(
        (120, 760), "29 Oct 2026, 04:15 PM", font=ImageFont.truetype(str(PLAIN), 40), fill="gray"
    )
    d.text(
        (120, 900),
        "UPI Ref No: 616292057492",
        font=ImageFont.truetype(str(PLAIN), 40),
        fill="black",
    )
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("amount", ["1", "99", "199", "299"])
@pytest.mark.parametrize("big", [60, 110, 160])
async def test_the_headline_amount_is_never_taken_for_the_date(amount, big):
    text = await TesseractOcr().read(receipt(amount, big), 1080, 1900)
    expected = Expected(int(amount) * 100, UPI, None, NOW - timedelta(minutes=8), NOW)
    a = analyze_text(text, expected, "tesseract")
    assert "amount_mismatch" not in a.findings, text
    if a.verdict == OK:
        assert a.amount_minor == int(amount) * 100
