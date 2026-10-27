"""Admin-created customers and orders in BigQuery suffai_ops (the tables every dashboard reads).

An order is created before its photos: it carries total_number_of_clothes as the expected count and has
no garment items until photos named {customer_id}_{order_id}_{item_id}.jpg reach the bucket.
Name, phone and address are admin-only and never logged; logs carry short ids.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

import admin_intel
import auth_acl
import pin_areas
import rfm
import vocab

log = logging.getLogger("command_center.ops_records")

GENDERS = ("Female", "Male", "Other")
NEW_SEGMENT = rfm.NEW
PHOTO_PATTERN = "garments/{customer_id}_{order_id}_{item_id}.jpg"
_PIN_RE = re.compile(r"^[1-9]\d{5}$")
_TEXT_RE = re.compile(r"^[\w\s&/.,'()+#:-]*$")
_SERVICE_RE = re.compile(r"^[\w\s&/.+-]{2,60}$")

Param = tuple[str, str, Any]

# Test hook: (sql, params) -> rows instead of BigQuery.
_runner: Optional[Callable[[str, list[Param]], list[dict[str, Any]]]] = None


class RecordError(ValueError):
    """Invalid input (4xx); the message is shown to the admin."""


class RecordsUnavailable(RuntimeError):
    pass


def _project() -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
    if not project:
        raise RecordsUnavailable("GOOGLE_CLOUD_PROJECT not set")
    return project


def _table(name: str) -> str:
    return f"`{_project()}.{os.getenv('BQ_DATASET', 'suffai_ops')}.{name}`"


def _param(bigquery: Any, name: str, typ: str, value: Any) -> Any:
    """ScalarQueryParameter, or for typ "STRUCTS" a list of dicts of strings (ARRAY<STRUCT<...>>)."""
    if typ != "STRUCTS":
        return bigquery.ScalarQueryParameter(name, typ, value)
    rows = [bigquery.StructQueryParameter(None, *[bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in r.items()])
            for r in value]
    return bigquery.ArrayQueryParameter(name, "STRUCT", rows)


def _run(sql: str, params: list[Param]) -> list[dict[str, Any]]:
    if _runner is not None:
        return _runner(sql, params)
    try:
        from google.cloud import bigquery  # type: ignore

        client = bigquery.Client(project=_project())
        cfg = bigquery.QueryJobConfig(query_parameters=[_param(bigquery, n, t, v) for n, t, v in params])
        return [dict(r) for r in client.query(sql, job_config=cfg).result()]
    except RecordsUnavailable:
        raise
    except Exception as exc:
        raise RecordsUnavailable(f"BigQuery write failed: {type(exc).__name__}") from exc


def new_id() -> str:
    """24 hex characters, the same shape as the existing customer / order ids (no '_', safe in photo names)."""
    return secrets.token_hex(12)


def photo_name(customer_id: str, order_id: str, item_id: str = "<item_id>") -> str:
    return PHOTO_PATTERN.format(customer_id=customer_id, order_id=order_id, item_id=item_id)


def upload_prefix() -> Optional[str]:
    """Bucket root; upload_prefix() + photo_name(...) is the full gs:// path."""
    bucket = (os.getenv("GCS_IMAGE_BUCKET") or "").strip()
    return f"gs://{bucket}/" if bucket else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _text(v: Any, field: str, *, lo: int, hi: int) -> str:
    s = re.sub(r"\s+", " ", str(v if v is not None else "")).strip()
    if not lo <= len(s) <= hi:
        raise RecordError(f"{field} must be {lo}-{hi} characters")
    if not _TEXT_RE.match(s):
        raise RecordError(f"{field} has unsupported characters")
    return s


def _phone(v: Any) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) != 10 or d[0] not in "6789":
        raise RecordError("Phone must be a 10-digit Indian mobile number")
    return "91" + d


def _int(v: Any, field: str, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise RecordError(f"{field} must be a whole number") from None
    if not lo <= n <= hi:
        raise RecordError(f"{field} must be between {lo} and {hi}")
    return n


def _float(v: Any, field: str, lo: float, hi: float) -> float:
    try:
        n = float(str(v).strip())
    except (TypeError, ValueError):
        raise RecordError(f"{field} must be a number") from None
    if not lo <= n <= hi:
        raise RecordError(f"{field} must be between {lo:g} and {hi:g}")
    return round(n, 2)


def _choice(v: Any, field: str, allowed: list[str]) -> str:
    s = str(v or "").strip()
    match = next((a for a in allowed if a.lower() == s.lower()), None)
    if match is None:
        raise RecordError(f"{field} must be one of: {', '.join(allowed)}")
    return match


def _date(v: Any) -> date:
    if v in (None, ""):
        return date.today()
    try:
        d = date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        raise RecordError("Scheduled date must be YYYY-MM-DD") from None
    if abs((d - date.today()).days) > 366:
        raise RecordError("Scheduled date must be within a year of today")
    return d


def form_options() -> dict[str, list[str]]:
    """Dropdown values for the Admin forms, from the data the dashboards already use."""
    data = admin_intel.dataset()
    services = sorted({o["service"] for rows in data["orders_by"].values() for o in rows
                       if admin_intel._known(o["service"]) and o["service"] != "Unspecified"})
    return {
        "genders": list(GENDERS),
        "services": services[:60],
        "pin_codes": sorted({c["pin_code"] for c in data["customers"].values() if c["pin_code"]}),
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _customer_exists(customer_id: str) -> bool:
    rows = _run(
        f"SELECT CAST(_id AS STRING) AS id FROM {_table('customers')} WHERE CAST(_id AS STRING) = @id LIMIT 1",
        [("id", "STRING", customer_id)],
    )
    return bool(rows)


def _gender(v: Any) -> str:
    g = vocab.canonical("customer_gender", v, default="")
    if g not in GENDERS:
        raise RecordError(f"Gender must be one of: {', '.join(GENDERS)}")
    return g


def create_customer(values: dict[str, Any], *, admin: str = "?", request_id: str = "-") -> dict[str, Any]:
    """New customer in segment New (recompute_rfm assigns the real segment once orders exist).

    The PIN must be verified by Gemini + Google Search (pin_areas, status ok) and its centre becomes the
    customer's lat / lon; the create is blocked with the reason otherwise.
    """
    pin = str(values.get("pin_code") or "").strip()
    if not _PIN_RE.match(pin):
        raise RecordError("PIN code must be 6 digits")
    row = {
        "id": new_id(),
        "name": _text(values.get("name"), "Name", lo=2, hi=80),
        "number": _phone(values.get("phone")),
        "address": _text(values.get("address"), "Address", lo=5, hi=200),
        "age": _int(values.get("age"), "Age", 10, 100),
        "gender": _gender(values.get("gender")),
        "pin_code": pin,
        "rfm_segment": NEW_SEGMENT,
    }
    try:
        area = pin_areas.require_ok(pin, request_id=request_id)
    except pin_areas.AreaRejected as exc:
        raise RecordError(str(exc)) from None
    row["lat"], row["lon"] = float(area["centroid_lat"]), float(area["centroid_lon"])
    _run(
        f"INSERT INTO {_table('customers')} "
        "(_id, name, number, address, age, gender, pin_code, rfm_segment, lat, lon, source, created_at) "
        "VALUES (@id, @name, @number, @address, @age, @gender, @pin_code, @rfm_segment, @lat, @lon, "
        "'admin', CURRENT_TIMESTAMP())",
        [("id", "STRING", row["id"]), ("name", "STRING", row["name"]), ("number", "STRING", row["number"]),
         ("address", "STRING", row["address"]), ("age", "INT64", row["age"]), ("gender", "STRING", row["gender"]),
         ("pin_code", "STRING", pin), ("rfm_segment", "STRING", row["rfm_segment"]),
         ("lat", "FLOAT64", row["lat"]), ("lon", "FLOAT64", row["lon"])],
    )
    log.info("admin=%s action=create_customer customer=%s pin=%s", admin, admin_intel.short_id(row["id"]), pin)
    out = {"customer_id": row["id"], "short_id": admin_intel.short_id(row["id"]), "pin_code": pin,
           "area_label": area.get("area_label"), "rfm_segment": row["rfm_segment"]}
    if values.get("create_login") is True:
        try:
            out["login"] = create_customer_login(row["id"], admin=admin)
        except RecordsUnavailable:
            out["login_error"] = "The customer was saved, but the Style Assistant login could not be created."
    return out


def create_customer_login(customer_id: str, *, admin: str = "?") -> dict[str, str]:
    """Style Assistant login cust-<short_id> with a generated password.

    Only the sha256 is stored in login_cred_db; the password is returned once and never logged.
    """
    table = _table("login_cred_db")
    username = f"cust-{admin_intel.short_id(customer_id)}"
    taken = _run(f"SELECT username FROM {table} WHERE username = @username LIMIT 1", [("username", "STRING", username)])
    if taken:
        username = f"cust-{customer_id}"
    password = secrets.token_urlsafe(9)
    _run(
        f"INSERT INTO {table} (username, password_hash, role, brand_name, customer_id, display_name, active, created_at) "
        "VALUES (@username, @password_hash, 'customer', NULL, @customer_id, @display_name, TRUE, CURRENT_TIMESTAMP())",
        [("username", "STRING", username), ("password_hash", "STRING", auth_acl.sha256_hex(password)),
         ("customer_id", "STRING", customer_id),
         ("display_name", "STRING", f"Customer {admin_intel.short_id(customer_id)}")],
    )
    log.info("admin=%s action=create_login customer=%s", admin, admin_intel.short_id(customer_id))
    return {"username": username, "password": password}


def create_order(values: dict[str, Any], *, admin: str = "?") -> dict[str, Any]:
    customer_id = str(values.get("customer_id") or "").strip()
    if not re.match(r"^[A-Za-z0-9-]{1,64}$", customer_id):
        raise RecordError("Choose a customer")
    if not _customer_exists(customer_id):
        raise RecordError("Customer not found")
    service = str(values.get("service") or "").strip()
    if not _SERVICE_RE.match(service):
        raise RecordError("Service must be 2-60 characters")
    row = {
        "id": new_id(),
        "customer_id": customer_id,
        "scheduled_date": _date(values.get("scheduled_date")),
        "service": service,
        "clothes": _int(values.get("total_number_of_clothes"), "Number of clothes", 1, 500),
        "amount": _float(values.get("amount") if values.get("amount") not in (None, "") else 0, "Amount", 0, 1_000_000),
    }
    _run(
        f"INSERT INTO {_table('orders')} "
        "(_id, customer_id, scheduled_date, service, total_number_of_clothes, amount, source, created_at) "
        "VALUES (@id, @customer_id, @scheduled_date, @service, @clothes, @amount, 'admin', CURRENT_TIMESTAMP())",
        [("id", "STRING", row["id"]), ("customer_id", "STRING", customer_id),
         ("scheduled_date", "DATE", row["scheduled_date"]), ("service", "STRING", service),
         ("clothes", "INT64", row["clothes"]), ("amount", "FLOAT64", row["amount"])],
    )
    log.info("admin=%s action=create_order customer=%s order=%s clothes=%s", admin,
             admin_intel.short_id(customer_id), admin_intel.short_id(row["id"]), row["clothes"])
    return {
        "order_id": row["id"],
        "customer_id": customer_id,
        "scheduled_date": row["scheduled_date"].isoformat(),
        "photos_expected": row["clothes"],
        "photos_received": 0,
        "photo_name": photo_name(customer_id, row["id"]),
        "upload_prefix": upload_prefix(),
    }


def recompute_rfm(*, apply: bool = True, admin: str = "system") -> dict[str, Any]:
    """Recompute every customer's rfm_segment from orders (rule rfm-v1); writes only the changed rows."""
    customers = _run(f"SELECT CAST(_id AS STRING) AS id, rfm_segment FROM {_table('customers')}", [])
    orders = _run(
        f"SELECT CAST(_id AS STRING) AS _id, CAST(customer_id AS STRING) AS customer_id, scheduled_date, amount "
        f"FROM {_table('orders')}",
        [],
    )
    segs = rfm.segments([c["id"] for c in customers], orders)
    changed = [{"id": c["id"], "seg": segs[c["id"]]} for c in customers if segs[c["id"]] != c.get("rfm_segment")]
    if changed and apply:
        _run(
            f"MERGE {_table('customers')} t USING (SELECT id, seg FROM UNNEST(@rows)) s "
            "ON CAST(t._id AS STRING) = s.id WHEN MATCHED THEN UPDATE SET rfm_segment = s.seg",
            [("rows", "STRUCTS", changed)],
        )
    by_segment: dict[str, int] = {}
    for c in customers:
        by_segment[segs[c["id"]]] = by_segment.get(segs[c["id"]], 0) + 1
    log.info("admin=%s action=recompute_rfm customers=%d changed=%d applied=%s", admin, len(customers),
             len(changed), bool(changed and apply))
    return {"rule_version": rfm.RULE_VERSION, "customers": len(customers), "changed": len(changed),
            "applied": bool(changed and apply), "by_segment": by_segment}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_orders(*, customer_id: Optional[str] = None, pending_only: bool = False,
                page: int = 1, size: int = 20) -> dict[str, Any]:
    """Orders with photo progress, newest first (admin-created orders by creation time)."""
    data = admin_intel.dataset()
    customers = data["customers"]
    rows: list[dict[str, Any]] = []
    for cid, orders in data["orders_by"].items():
        if customer_id and cid != customer_id:
            continue
        owner = customers.get(cid)
        for o in orders:
            item = admin_intel.order_row(o)
            if pending_only and not item["photos_pending"]:
                continue
            rows.append({
                **item,
                "customer_id": cid,
                "customer_short_id": admin_intel.short_id(cid),
                "customer_name": owner["name"] if owner else None,
                "pin_code": owner["pin_code"] if owner else None,
                "photo_name": photo_name(cid, o["order_id"]),
                "_created": o.get("created_at") or "",
            })
    rows.sort(key=lambda r: (r["_created"], r["date"] or ""), reverse=True)
    size = max(1, min(int(size), 100))
    pages = max(1, -(-len(rows) // size))
    page = max(1, min(int(page), pages))
    chunk = rows[(page - 1) * size: page * size]
    for r in chunk:
        r.pop("_created", None)
    return {
        "total": len(rows),
        "page": page,
        "size": size,
        "items": chunk,
        "photo_pattern": PHOTO_PATTERN,
        "upload_prefix": upload_prefix(),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
