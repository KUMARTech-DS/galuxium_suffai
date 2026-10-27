"""Input checks shared by the retail modules. Each raises RetailError with a message for the user."""

from __future__ import annotations

import re
from typing import Any, Optional

import retail_gst as gst
from retail_db import RetailError

_TEXT_RE = re.compile(r"^[^\x00-\x1f<>]*$")
_CODE_RE = re.compile(r"^[A-Z0-9]{2,5}$")
_BIN_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,15}$")
_PIN_RE = re.compile(r"^[1-9]\d{5}$")
_STYLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}$")
_SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{0,47}$")
_BARCODE_RE = re.compile(r"^[A-Za-z0-9-]{4,32}$")
_USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_QTY = 100_000
MAX_LINES = 500
DAMAGED_BIN = "DMG"


def text(v: Any, field: str, *, hi: int, required: bool = True, lo: int = 1) -> Optional[str]:
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    if not s:
        if required:
            raise RetailError(f"{field} is required.")
        return None
    if len(s) < lo or len(s) > hi:
        raise RetailError(f"{field} must be {lo}-{hi} characters.")
    if not _TEXT_RE.match(s):
        raise RetailError(f"{field} contains characters that aren't allowed.")
    return s


def location_code(v: Any) -> str:
    s = str(v or "").strip().upper()
    if not _CODE_RE.match(s):
        raise RetailError("Location code must be 2-5 letters or digits, e.g. BLR01. It starts every invoice number.")
    return s


def bin_code(v: Any, *, allow_blank: bool = True) -> str:
    s = str(v or "").strip().upper()
    if not s:
        if allow_blank:
            return ""
        raise RetailError("Bin code is required.")
    if not _BIN_RE.match(s):
        raise RetailError("Bin codes are up to 16 letters, digits or dashes, e.g. A-01-03.")
    return s


def pin_code(v: Any, *, required: bool) -> Optional[str]:
    s = str(v or "").strip()
    if not s:
        if required:
            raise RetailError("PIN code is required for stores (Geo and campaigns use it).")
        return None
    if not _PIN_RE.match(s):
        raise RetailError("PIN code must be 6 digits.")
    return s


def state_code(v: Any) -> str:
    s = str(v or "").strip().zfill(2)
    if s not in gst.STATES:
        raise RetailError("Pick the GST state of this location.")
    return s


def gstin(v: Any, *, state: Optional[str] = None) -> Optional[str]:
    try:
        g = gst.normalize_gstin(v)
    except ValueError as exc:
        raise RetailError(str(exc)) from None
    if g and state and g[:2] != state:
        raise RetailError(f"This GSTIN is registered in {gst.STATES[g[:2]]}, not {gst.STATES[state]}.")
    return g


def style_code(v: Any) -> str:
    s = str(v or "").strip()
    if not _STYLE_RE.match(s):
        raise RetailError("Style code must be 1-40 letters, digits, dots, dashes or slashes.")
    return s


def sku(v: Any) -> str:
    s = str(v or "").strip().upper()
    if not _SKU_RE.match(s):
        raise RetailError("SKU must be 1-48 letters, digits, dots, dashes or slashes.")
    return s


def barcode(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    if not s:
        return None
    if not _BARCODE_RE.match(s):
        raise RetailError("Barcode must be 4-32 letters or digits.")
    return s


def size(v: Any) -> str:
    s = re.sub(r"\s+", "", str(v or "")).upper()
    if not s or len(s) > 12:
        raise RetailError("Size is required (up to 12 characters, e.g. M, XL, 32, FREE).")
    return s


def qty(v: Any, field: str = "Quantity", *, lo: int = 1, hi: int = MAX_QTY) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise RetailError(f"{field} must be a whole number.") from None
    if n < lo or n > hi:
        raise RetailError(f"{field} must be between {lo} and {hi}.")
    return n


def signed_qty(v: Any, field: str = "Quantity") -> int:
    n = qty(v, field, lo=-MAX_QTY, hi=MAX_QTY)
    if n == 0:
        raise RetailError(f"{field} can't be 0.")
    return n


def money(v: Any, field: str, *, lo_paise: int = 0, hi_paise: int = 100_000_000) -> int:
    try:
        p = gst.to_paise(v, field)
    except ValueError as exc:
        raise RetailError(str(exc)) from None
    if p < lo_paise or p > hi_paise:
        raise RetailError(f"{field} must be between ₹{lo_paise / 100:,.2f} and ₹{hi_paise / 100:,.0f}.")
    return p


def percent_bp(v: Any, field: str = "Discount") -> int:
    if v in (None, ""):
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise RetailError(f"{field} must be a percentage.") from None
    if not 0 <= f <= 100:
        raise RetailError(f"{field} must be between 0% and 100%.")
    return int(round(f * 100))


def phone(v: Any) -> Optional[str]:
    d = re.sub(r"\D", "", str(v or ""))
    if not d:
        return None
    if len(d) > 10 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) != 10 or d[0] not in "6789":
        raise RetailError("Enter a 10-digit Indian mobile number.")
    return d


def mask_phone(p: Optional[str]) -> Optional[str]:
    return ("•" * 6 + p[-4:]) if p else None


def username(v: Any) -> str:
    s = str(v or "").strip().lower()
    if not _USER_RE.match(s):
        raise RetailError("Username must be 3-32 lowercase letters, digits, dots, dashes or underscores.")
    return s


def iso_date(v: Any, field: str, *, required: bool = False) -> Optional[str]:
    s = str(v or "").strip()
    if not s:
        if required:
            raise RetailError(f"{field} is required.")
        return None
    if not _DATE_RE.match(s):
        raise RetailError(f"{field} must be a date like 2026-10-20.")
    from datetime import date

    try:
        date.fromisoformat(s)
    except ValueError:
        raise RetailError(f"{field} is not a real date.") from None
    return s


def lines(v: Any, field: str = "lines") -> list[dict[str, Any]]:
    if not isinstance(v, list) or not v:
        raise RetailError("Add at least one item.")
    if len(v) > MAX_LINES:
        raise RetailError(f"At most {MAX_LINES} items per document.")
    if not all(isinstance(x, dict) for x in v):
        raise RetailError(f"Each of {field} must be an object.")
    return v


def page(page_no: Any, size_: Any, *, max_size: int = 200) -> tuple[int, int]:
    try:
        p, s = max(1, int(page_no or 1)), int(size_ or 50)
    except (TypeError, ValueError):
        p, s = 1, 50
    return p, min(max(s, 1), max_size)
