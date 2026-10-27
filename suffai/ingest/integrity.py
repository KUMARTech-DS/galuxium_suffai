"""Referential integrity and source validation report (ING-01)."""

from __future__ import annotations

from collections import Counter
from typing import Any


def referential_integrity_report(normalized: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    customers = normalized.get("customers", [])
    orders = normalized.get("orders", [])
    stores = normalized.get("stores", [])
    services = normalized.get("services", [])
    commodities = normalized.get("commodities", [])
    lines = normalized.get("order_commodity_lines", [])

    cust_ids = {c["customer_id"] for c in customers if c.get("customer_id")}
    user_ids = {c["user_id"] for c in customers if c.get("user_id")}
    store_ids = {s["store_id"] for s in stores if s.get("store_id")}
    service_ids = {s["service_id"] for s in services if s.get("service_id")}
    commodity_ids = {c["catalog_commodity_id"] for c in commodities if c.get("catalog_commodity_id")}

    order_cust = [o.get("customer_id") for o in orders]
    match_id = sum(1 for x in order_cust if x in cust_ids)
    wrong_user = sum(1 for x in order_cust if x in user_ids)
    unmatched_cust = sum(1 for x in order_cust if x and x not in cust_ids)
    match_store = sum(1 for o in orders if o.get("store_id") in store_ids)

    line_qty = sum(int(l.get("quantity") or 0) for l in lines)
    null_bill = sum(1 for o in orders if not o.get("bill_no"))
    null_pin = sum(1 for o in orders if not o.get("order_pin"))
    statuses = Counter(o.get("order_status") or "null" for o in orders)
    payments = Counter(o.get("payment_status") or "null" for o in orders)

    orphan_lines_commodity = sum(
        1 for l in lines
        if l.get("catalog_commodity_id") and l["catalog_commodity_id"] not in commodity_ids
    )
    orphan_lines_service = sum(
        1 for l in lines
        if l.get("service_id") and l["service_id"] not in service_ids
    )

    return {
        "counts": {
            "customers": len(customers),
            "orders": len(orders),
            "stores": len(stores),
            "services": len(services),
            "commodities": len(commodities),
            "order_commodity_lines": len(lines),
            "order_commodity_quantity_sum": line_qty,
        },
        "joins": {
            "orders_customer_id_to_customers_id": {
                "matched": match_id,
                "unmatched": unmatched_cust,
                "total": len(orders),
            },
            "orders_customer_id_to_customers_user_id_WRONG": {
                "matched": wrong_user,
                "note": "Must be ~0; never use this join for production.",
            },
            "orders_store_id_to_stores": {"matched": match_store, "total": len(orders)},
            "lines_orphan_catalog_commodity": orphan_lines_commodity,
            "lines_orphan_service": orphan_lines_service,
        },
        "nulls": {
            "orders_null_bill_no": null_bill,
            "orders_null_schedule_pin": null_pin,
        },
        "distributions": {
            "order_status": dict(statuses.most_common()),
            "payment_status": dict(payments.most_common()),
        },
        "gates": {
            "correct_customer_join_majority": match_id > len(orders) * 0.9,
            "wrong_user_id_join_near_zero": wrong_user == 0,
            "lines_exist": len(lines) > 0,
        },
    }
