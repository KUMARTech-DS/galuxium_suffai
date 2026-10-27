"""Normalize source documents into flat facts. Preserves nested line IDs."""

from __future__ import annotations

from typing import Any

from suffai.ids import extract_datetime, extract_id, normalize_pin


def normalize_customers(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for doc in raw:
        rows.append({
            "customer_id": extract_id(doc.get("_id")),
            "user_id": extract_id(doc.get("user_id")),
            "customer_name": doc.get("customer_name"),
            "phone_no": doc.get("phone_no"),
            "email_address": doc.get("email_address"),
            "gender": doc.get("gender") or None,
            "dob": extract_datetime(doc.get("dob")),
            "home_pin": normalize_pin(doc.get("pin_code")),
            "created_at": extract_datetime(doc.get("created_at")),
            "updated_at": extract_datetime(doc.get("updated_at")),
        })
    return [r for r in rows if r["customer_id"]]


def normalize_stores(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for doc in raw:
        rows.append({
            "store_id": extract_id(doc.get("_id")),
            "store_name": doc.get("store_name"),
            "pin_code": normalize_pin(doc.get("pin_code")),
            "city": doc.get("city"),
            "area": doc.get("area"),
            "premium_laundry": bool(doc.get("premium_laundry")),
            "store_category": doc.get("suffai_store_base_category"),
            "status": doc.get("status"),
        })
    return [r for r in rows if r["store_id"]]


def normalize_services(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "service_id": extract_id(doc.get("_id")),
            "service_name": doc.get("service_name"),
        }
        for doc in raw
        if extract_id(doc.get("_id"))
    ]


def normalize_commodities(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "catalog_commodity_id": extract_id(doc.get("_id")),
            "name": doc.get("name"),
            "menu": doc.get("menu"),
            "service_id": extract_id(doc.get("service_id")),
            "image_url": doc.get("image_url"),  # catalog imagery, NOT customer garment
        }
        for doc in raw
        if extract_id(doc.get("_id"))
    ]


def _order_pin(doc: dict[str, Any]) -> str | None:
    sch = doc.get("schedule")
    if isinstance(sch, dict):
        addr = sch.get("address")
        if isinstance(addr, dict):
            return normalize_pin(addr.get("pin_code"))
    return None


def normalize_orders(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for doc in raw:
        oid = extract_id(doc.get("_id"))
        if not oid:
            continue
        bill = doc.get("bill_number")
        if bill is not None:
            bill = str(bill).strip() or None
        rows.append({
            "order_id": oid,
            "customer_id": extract_id(doc.get("customer_id")),
            "store_id": extract_id(doc.get("store_id")),
            "bill_no": bill,
            "order_status": str(doc.get("order_status") or "").strip() or None,
            "payment_status": str(doc.get("payment_status") or "").strip() or None,
            "refund_flag": bool(doc.get("refund_flag")),
            "total_amount": _num(doc.get("total_amount")),
            "sub_total_amount": _num(doc.get("sub_total_amount")),
            "ordered_at": extract_datetime(doc.get("order_date_time") or doc.get("created_at")),
            "order_pin": _order_pin(doc),
            "total_number_of_clothes": _int(doc.get("total_number_of_clothes")),
        })
    return rows


def normalize_order_commodity_lines(raw_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """UNNEST orderItems × orderedAccommodities. Quantity lines ≠ garment instances."""
    lines: list[dict[str, Any]] = []
    for doc in raw_orders:
        order_id = extract_id(doc.get("_id"))
        if not order_id:
            continue
        customer_id = extract_id(doc.get("customer_id"))
        store_id = extract_id(doc.get("store_id"))
        order_pin = _order_pin(doc)
        ordered_at = extract_datetime(doc.get("order_date_time") or doc.get("created_at"))
        items = doc.get("orderItems") or []
        if not isinstance(items, list):
            continue
        for itm in items:
            if not isinstance(itm, dict):
                continue
            order_item_id = extract_id(itm.get("_id"))
            service_id = extract_id(itm.get("service_id"))
            service_name = itm.get("name")
            accs = itm.get("orderedAccommodities") or []
            if not isinstance(accs, list) or not accs:
                continue
            for acc in accs:
                if not isinstance(acc, dict):
                    continue
                line_id = extract_id(acc.get("_id"))
                # Source spelling: accomodityId (missing 'm')
                catalog_id = extract_id(acc.get("accomodityId") or acc.get("commodityId"))
                qty = _int(acc.get("quantity")) or 0
                lines.append({
                    "line_id": line_id,
                    "order_id": order_id,
                    "order_item_id": order_item_id,
                    "customer_id": customer_id,
                    "store_id": store_id,
                    "service_id": service_id,
                    "service_name": service_name,
                    "catalog_commodity_id": catalog_id,
                    "commodity_name": acc.get("commodityName") or acc.get("name"),
                    "menu": acc.get("menu"),
                    "quantity": qty,
                    "line_price": _num(acc.get("price")),
                    "order_pin": order_pin,
                    "ordered_at": ordered_at,
                })
    return lines


def _num(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def normalize_all(raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "customers": normalize_customers(raw.get("customers", [])),
        "stores": normalize_stores(raw.get("stores", [])),
        "services": normalize_services(raw.get("services", [])),
        "commodities": normalize_commodities(raw.get("commodities", [])),
        "orders": normalize_orders(raw.get("orders", [])),
        "order_commodity_lines": normalize_order_commodity_lines(raw.get("orders", [])),
    }
