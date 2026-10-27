"""GST and money arithmetic for POS invoices and credit notes (pure, integer paise).

Apparel (HSN chapters 61 / 62): 5% when the sale value of a piece is up to Rs 2,500 and 18% above it
(GST Council, effective 22 September 2025). Sale value is the taxable value of one piece after
discount. Shelf prices (MRP) include GST, so taxable value = price * 100 / (100 + rate); the tax is the
rest, so a line's taxable value plus tax is always exactly what the shopper pays for it.
Intra-state sales split the tax equally into CGST and SGST; inter-state sales charge IGST.
The bill total is rounded to the nearest rupee and the difference shown as round-off.
"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any, Optional, Sequence

APPAREL_THRESHOLD_PAISE = int(float(os.getenv("RETAIL_GST_APPAREL_THRESHOLD_INR", "2500")) * 100)
APPAREL_LOW_BP = int(os.getenv("RETAIL_GST_APPAREL_LOW_BP", "500"))
APPAREL_HIGH_BP = int(os.getenv("RETAIL_GST_APPAREL_HIGH_BP", "1800"))
ALLOWED_FIXED_BP = (0, 250, 300, 500, 1200, 1800, 2800, 4000)

STATES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}

_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_HSN_RE = re.compile(r"^\d{4}(\d{2}){0,2}$")


def div_round(n: int, d: int) -> int:
    """n / d rounded half away from zero, in integers."""
    if d <= 0:
        raise ValueError("divisor must be positive")
    q, r = divmod(abs(n), d)
    q += 1 if 2 * r >= d else 0
    return q if n >= 0 else -q


def gstin_check_char(first14: str) -> str:
    factor, total = 2, 0
    for c in reversed(first14):
        a = factor * _GSTIN_CHARS.index(c)
        factor = 1 if factor == 2 else 2
        total += a // 36 + a % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def normalize_gstin(v: Any) -> Optional[str]:
    """Upper-cased GSTIN, None when blank. Raises ValueError when the format or check digit is wrong."""
    s = re.sub(r"\s", "", str(v or "")).upper()
    if not s:
        return None
    if not _GSTIN_RE.match(s) or s[:2] not in STATES:
        raise ValueError("GSTIN must be 15 characters like 29ABCDE1234F1Z5.")
    if gstin_check_char(s[:14]) != s[14]:
        raise ValueError("That GSTIN's check digit is wrong. Check it for a typo.")
    return s


def valid_hsn(v: Any) -> bool:
    return bool(_HSN_RE.match(str(v or "").strip()))


def apparel_rate_bp(value_paise: int, qty: int) -> int:
    """5% if the taxable value per piece at 5% is within the threshold, else 18%."""
    # value / qty * 10000 / (10000 + low) <= threshold, in integers
    if value_paise * 10000 <= APPAREL_THRESHOLD_PAISE * (10000 + APPAREL_LOW_BP) * qty:
        return APPAREL_LOW_BP
    return APPAREL_HIGH_BP


def rate_for(rule: str, fixed_bp: Optional[int], value_paise: int, qty: int) -> int:
    if rule == "fixed":
        return int(fixed_bp or 0)
    return apparel_rate_bp(value_paise, qty)


def price_line(*, mrp_paise: int, qty: int, discount_bp: int, rule: str, fixed_bp: Optional[int],
               inter_state: bool) -> dict[str, int]:
    """One invoice line from an MRP (GST inclusive), a quantity and a percentage discount (basis points)."""
    if qty <= 0:
        raise ValueError("quantity must be positive")
    if not 0 <= discount_bp <= 10000:
        raise ValueError("discount must be between 0% and 100%")
    gross = mrp_paise * qty
    discount = div_round(gross * discount_bp, 10000)
    value = gross - discount
    rate = rate_for(rule, fixed_bp, value, qty)
    if inter_state:
        igst, cgst, sgst = div_round(value * rate, 10000 + rate), 0, 0
    else:
        cgst = sgst = div_round(value * rate, 2 * (10000 + rate))
        igst = 0
    return {
        "qty": qty, "mrp_paise": mrp_paise, "gross_paise": gross, "discount_paise": discount,
        "taxable_paise": value - cgst - sgst - igst, "gst_rate_bp": rate, "cgst_paise": cgst,
        "sgst_paise": sgst, "igst_paise": igst, "total_paise": value,
    }


AMOUNT_KEYS = ("gross_paise", "discount_paise", "taxable_paise", "cgst_paise", "sgst_paise", "igst_paise",
               "total_paise")


def credit_line(original: dict[str, Any], *, qty: int, returned_before: int,
                credited_before: dict[str, int]) -> dict[str, int]:
    """Amounts to reverse for `qty` units of an original line. The return that brings the line to its
    full quantity takes whatever is left, so all credit notes together reverse the line exactly."""
    orig_qty = int(original["qty"])
    if qty <= 0 or returned_before + qty > orig_qty:
        raise ValueError("return quantity exceeds what is left on the line")
    out: dict[str, int] = {"qty": qty, "mrp_paise": int(original["mrp_paise"]),
                           "gst_rate_bp": int(original["gst_rate_bp"])}
    gross_orig = int(original.get("gross_paise") or int(original["mrp_paise"]) * orig_qty)
    full = {"gross_paise": gross_orig, **{k: int(original[k]) for k in AMOUNT_KEYS if k != "gross_paise"}}
    for k in AMOUNT_KEYS:
        if returned_before + qty == orig_qty:
            out[k] = full[k] - int(credited_before.get(k) or 0)
        else:
            out[k] = div_round(full[k] * qty, orig_qty)
    if returned_before + qty < orig_qty:
        out["total_paise"] = out["gross_paise"] - out["discount_paise"]
        out["taxable_paise"] = out["total_paise"] - out["cgst_paise"] - out["sgst_paise"] - out["igst_paise"]
    return out


def bill_totals(lines: Sequence[dict[str, int]]) -> dict[str, int]:
    """Sum of the lines; total rounded to the nearest rupee with the difference as round_off."""
    sums = {k: sum(int(ln[k]) for ln in lines) for k in AMOUNT_KEYS}
    units = sum(int(ln["qty"]) for ln in lines)
    exact = sums["total_paise"]
    total = div_round(exact, 100) * 100
    return {**sums, "units": units, "round_off_paise": total - exact, "total_paise": total}


def tax_by_rate(lines: Sequence[dict[str, Any]]) -> list[dict[str, int]]:
    groups: dict[int, dict[str, int]] = {}
    for ln in lines:
        g = groups.setdefault(int(ln["gst_rate_bp"]), {"gst_rate_bp": int(ln["gst_rate_bp"]), "taxable_paise": 0,
                                                       "cgst_paise": 0, "sgst_paise": 0, "igst_paise": 0})
        for k in ("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise"):
            g[k] += int(ln[k])
    return [groups[k] for k in sorted(groups)]


def financial_year(d: date) -> str:
    """Indian financial year label: 2627 for 1 Apr 2026 - 31 Mar 2027."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def invoice_number(location_code: str, fy: str, n: int, *, credit_note: bool = False) -> str:
    """At most 16 characters (GST rule): BLR012627-000123, credit notes BLR012627R000004."""
    sep = "R" if credit_note else "-"
    return f"{location_code}{fy}{sep}{n:06d}"


def document_number(prefix: str, fy: str, n: int) -> str:
    return f"{prefix}{fy}-{n:05d}"


_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _below_1000(n: int) -> str:
    h, rest = divmod(n, 100)
    parts = []
    if h:
        parts.append(_ONES[h] + " Hundred")
    if rest:
        parts.append(_below_100(rest))
    return " ".join(parts)


def _indian_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        q, n = divmod(n, size)
        if q:
            parts.append((_indian_words(q) if q >= 100 else _below_100(q)) + " " + name)
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(paise: int) -> str:
    rupees, p = divmod(abs(int(paise)), 100)
    words = "Rupees " + _indian_words(rupees)
    if p:
        words += " and " + _below_100(p) + " Paise"
    return words + " Only"


def rupees(paise: Any) -> float:
    return round(int(paise or 0) / 100, 2)


def to_paise(v: Any, field: str = "amount") -> int:
    """Rupees (number or numeric string, up to 2 decimals) to paise."""
    try:
        s = str(v).strip().replace(",", "")
        if not re.match(r"^-?\d+(\.\d{1,2})?$", s):
            raise ValueError
        whole, _, frac = s.lstrip("-").partition(".")
        p = int(whole) * 100 + int((frac + "00")[:2])
        return -p if s.startswith("-") else p
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a rupee amount like 1299 or 1299.50") from None
