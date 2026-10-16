"""UPI payment link + QR for a manual payment (P6). Pure helpers.

The QR only encodes a standard `upi://pay` link (payee, exact amount, note). Scanning it opens
the user's UPI app pre-filled; it proves nothing about whether money was sent.
"""

from __future__ import annotations

import io
import re
from decimal import Decimal
from urllib.parse import quote

import segno


def format_inr(amount_minor: int) -> str:
    """99 for ₹99, 99.50 for ₹99.50 (no currency symbol; texts add it)."""
    rupees, paise = divmod(amount_minor, 100)
    return f"{rupees}" if paise == 0 else f"{rupees}.{paise:02d}"


def upi_uri(upi_id: str, payee: str, amount_minor: int, reference: str) -> str:
    rupees, paise = divmod(amount_minor, 100)
    params = {
        "pa": upi_id,
        "pn": payee,
        "am": f"{rupees}.{paise:02d}",
        "cu": "INR",
        "tn": reference,
    }
    query = "&".join(f"{k}={quote(v, safe='@.-_')}" for k, v in params.items())
    return f"upi://pay?{query}"


def qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


_RUPEES = re.compile(r"^[0-9]{1,7}(?:\.[0-9]{1,2})?$")


def parse_rupees(raw: str) -> int | None:
    """'99', '99.5', '99.50' -> paise. None unless a positive amount with at most 2 decimals."""
    text = raw.strip()
    if not _RUPEES.fullmatch(text):
        return None
    paise = int((Decimal(text) * 100).to_integral_value())
    return paise if paise > 0 else None
