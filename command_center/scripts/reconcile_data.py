#!/usr/bin/env python3
"""Remove rows that point at customers or orders that no longer exist, and check that every matched photo
reached BigQuery, so Admin, Geo, Ads Lab and the campaign ranking agree.

Garments per PIN are counted from BigQuery (suffai_ops.garment_items) only; Cloud SQL keeps the
embeddings and each photo's similarity event.

Dry run by default: prints what would change. --apply makes the changes.

BigQuery suffai_ops:
  orders whose customer is missing                     deleted
  garment items whose order or customer is missing     deleted
  similarity observations of missing customers         deleted
  customer logins whose customer is missing            active = FALSE
Cloud SQL (DATABASE_URL; --skip-cloud-sql to leave it alone):
  user_wardrobe / campaign_user_recommendations rows of missing customers   deleted
  user_wardrobe.pin_code_at_first_sight                set to the customer's current PIN
  observations waiting for BigQuery                    reported (suffai-similarity resends them)
  matched photos with no BigQuery observation          queued for resend (bq_synced_at cleared)
  observed garments missing from unique_garments       reported (compare-pins has no embedding for them)
  unique_garments with no wardrobe link left           reported only, never deleted

  python3 command_center/scripts/reconcile_data.py --project P [--dataset suffai_ops] [--apply] [--skip-cloud-sql]

Safe while photos are uploading: a resend writes the same observation row again and garment_items keeps one.
Queued photos are resent by suffai-similarity, up to 10 after each photo it processes.
Only counts and garment ids are printed (no customer data).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

import ops_records  # noqa: E402
from similarity.photo_key import parse_photo_key  # noqa: E402

_PIN_RE = re.compile(r"^[1-9]\d{5}$")
OBS_CUSTOMER = "COALESCE(NULLIF(x.customer_id, ''), x.user_uuid)"
UNLINKED_SHOWN = 10
# Same as similarity.db.SYNC_OUTCOMES: events that write a BigQuery observation row.
SYNC_OUTCOMES = ["user_match", "global_match_new_user_link", "created_new", "user_sku_match", "global_sku_match"]
EVENT_SYNC_COLUMNS = ("order_id", "item_id", "bq_synced_at")

# Test hook: () -> DB-API connection (cursor / commit / rollback / close) instead of psycopg2.
_pg_connect: Optional[Callable[[], Any]] = None


def _t(name: str) -> str:
    return ops_records._table(name)


def _has_customer(col: str) -> str:
    return f"EXISTS (SELECT 1 FROM {_t('customers')} c WHERE CAST(c._id AS STRING) = CAST({col} AS STRING))"


def bq_steps() -> list[dict[str, Any]]:
    """BigQuery changes, in run order. `where` selects the rows of `table` (alias x) to change."""
    live_order = (f"EXISTS (SELECT 1 FROM {_t('orders')} o WHERE CAST(o._id AS STRING) = CAST(x.order_id AS STRING) "
                  f"AND {_has_customer('o.customer_id')})")
    return [
        {"key": "orders", "label": "orders whose customer is missing", "table": "orders",
         "where": f"NOT {_has_customer('x.customer_id')}", "set": None},
        {"key": "items", "label": "garment items whose order or customer is missing", "table": "commodities",
         "where": f"NOT {_has_customer('x.customer_id')} OR NOT {live_order}", "set": None},
        {"key": "observations", "label": "similarity observations of missing customers",
         "table": "garment_similarity_observations", "where": f"NOT {_has_customer(OBS_CUSTOMER)}", "set": None},
        {"key": "logins", "label": "active customer logins whose customer is missing", "table": "login_cred_db",
         "where": "x.role = 'customer' AND IFNULL(x.active, TRUE) AND IFNULL(x.customer_id, '') != '' "
                  f"AND NOT {_has_customer('x.customer_id')}", "set": "active = FALSE"},
    ]


def _statement(step: dict[str, Any]) -> str:
    table = _t(step["table"])
    if step["set"]:
        return f"UPDATE {table} x SET {step['set']} WHERE {step['where']}"
    return f"DELETE FROM {table} x WHERE {step['where']}"


def customer_count() -> int:
    return int(ops_records._run(f"SELECT COUNT(*) AS n FROM {_t('customers')}", [])[0]["n"])


def reconcile_bigquery(*, apply: bool) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for step in bq_steps():
        res: dict[str, Any] = {"label": step["label"], "rows": None, "applied": False, "error": None,
                               "change": "deactivated" if step["set"] else "deleted"}
        out[step["key"]] = res
        try:
            rows = ops_records._run(f"SELECT COUNT(*) AS n FROM {_t(step['table'])} x WHERE {step['where']}", [])
            res["rows"] = int(rows[0]["n"]) if rows else 0
            if apply and res["rows"]:
                ops_records._run(_statement(step), [])
                res["applied"] = True
        except ops_records.RecordsUnavailable as exc:
            res["error"] = str(exc)
    return out


def bigquery_state() -> tuple[dict[str, str], dict[str, set]]:
    """(customer id -> current PIN, {"garments": observed garment ids, "items": observed (order_id, item_id)}).

    "items" are the photo items garment_items can link (observations with an order and item id).
    """
    customers = {
        str(r["id"]): str(r.get("pin") or "").strip()
        for r in ops_records._run(
            f"SELECT CAST(_id AS STRING) AS id, TRIM(CAST(pin_code AS STRING)) AS pin FROM {_t('customers')} "
            "WHERE _id IS NOT NULL",
            [],
        )
    }
    obs = _t("garment_similarity_observations")
    garments = ops_records._run(
        f"SELECT DISTINCT LOWER(x.garment_id) AS garment_id FROM {obs} x"
        f" JOIN {_t('customers')} c ON CAST(c._id AS STRING) = {OBS_CUSTOMER}"
        " WHERE IFNULL(x.garment_id, '') != ''",
        [],
    )
    items = ops_records._run(
        f"SELECT DISTINCT order_id, item_id FROM {obs} WHERE order_id IS NOT NULL AND item_id IS NOT NULL",
        [],
    )
    return customers, {
        "garments": {str(r["garment_id"]) for r in garments},
        "items": {(str(r["order_id"]), str(r["item_id"])) for r in items},
    }


@contextmanager
def _connect(*, commit: bool) -> Iterator[Any]:
    if _pg_connect is not None:
        conn = _pg_connect()
    else:
        dsn = (os.getenv("DATABASE_URL") or "").strip()
        if not dsn:
            raise SystemExit("DATABASE_URL is not set (or pass --skip-cloud-sql)")
        import psycopg2  # type: ignore

        conn = psycopg2.connect(dsn)
    try:
        yield conn
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_cloud_sql(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT user_uuid, garment_id::text, pin_code_at_first_sight FROM user_wardrobe")
        wardrobe = [(str(u), str(g).lower(), p) for u, g, p in cur.fetchall()]
        cur.execute("SELECT to_regclass('campaign_user_recommendations') IS NOT NULL")
        rec_users: dict[str, int] = {}
        if cur.fetchone()[0]:
            cur.execute("SELECT user_uuid, COUNT(*) FROM campaign_user_recommendations GROUP BY user_uuid")
            rec_users = {str(u): int(n) for u, n in cur.fetchall()}
        cur.execute("SELECT garment_id::text FROM unique_garments")
        garments = {str(r[0]).lower() for r in cur.fetchall()}
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'garment_similarity_events' AND column_name = ANY(%s)",
            (list(EVENT_SYNC_COLUMNS),),
        )
        events = None
        if int(cur.fetchone()[0]) == len(EVENT_SYNC_COLUMNS):
            cur.execute(
                "SELECT id::text, gcs_object_key, order_id, item_id, user_uuid, bq_synced_at IS NULL, created_at "
                "FROM garment_similarity_events WHERE outcome = ANY(%s) AND matched_garment_id IS NOT NULL "
                "ORDER BY created_at, id",
                (SYNC_OUTCOMES,),
            )
            events = list(cur.fetchall())
    return {"wardrobe": wardrobe, "rec_users": rec_users, "garments": garments, "events": events}


def plan_events(customers: dict[str, str], observed_items: set[tuple[str, str]],
                events: list[tuple]) -> dict[str, Any]:
    """Pending BigQuery sends, and the latest synced event of each photo item BigQuery doesn't have.

    events: (id, gcs_object_key, order_id, item_id, user_uuid, pending, created_at), oldest first.
    Photos whose name has no order and item are left alone: garment_items could not link a resend.
    """
    pending = [created for *_, is_pending, created in events if is_pending]
    latest: dict[tuple[str, str], tuple[str, str, bool]] = {}
    for event_id, key, order_id, item_id, user, is_pending, _ in events:
        photo = parse_photo_key(key)
        if photo and not (order_id and item_id):
            order_id, item_id = photo.order_id, photo.item_id
        if order_id and item_id:
            owner = str(user or (photo.customer_id if photo else ""))
            latest[(str(order_id), str(item_id))] = (event_id, owner, is_pending)
    requeue = sorted(event_id for item, (event_id, owner, is_pending) in latest.items()
                     if not is_pending and item not in observed_items and owner in customers)
    return {"pending_events": len(pending), "oldest_pending": min(pending) if pending else None,
            "requeue_events": requeue}


def plan_cloud_sql(customers: dict[str, str], observed: dict[str, set], *,
                   wardrobe: list[tuple[str, str, Optional[str]]], rec_users: dict[str, int],
                   garments: set[str], events: Optional[list[tuple]]) -> dict[str, Any]:
    gone = sorted({u for u, _, _ in wardrobe if u not in customers})
    rec_gone = sorted(u for u in rec_users if u not in customers)
    stale = [(u, customers[u]) for u, _, p in wardrobe
             if u in customers and _PIN_RE.match(customers[u]) and p != customers[u]]
    linked = {g for u, g, _ in wardrobe if u in customers}
    return {
        "wardrobe_users_gone": gone,
        "wardrobe_rows_gone": sum(1 for u, _, _ in wardrobe if u not in customers),
        "rec_users_gone": rec_gone,
        "rec_rows_gone": sum(rec_users[u] for u in rec_gone),
        "pin_updates": sorted(set(stale)),
        "pin_rows": len(stale),
        "events_tracked": events is not None,
        **(plan_events(customers, observed["items"], events) if events is not None
           else {"pending_events": 0, "oldest_pending": None, "requeue_events": []}),
        "garments_not_in_sql": len(observed["garments"] - garments),
        "unlinked_garments": sorted(garments - linked),
    }


def apply_cloud_sql(conn: Any, plan: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        if plan["wardrobe_users_gone"]:
            cur.execute("DELETE FROM user_wardrobe WHERE user_uuid = ANY(%s)", (plan["wardrobe_users_gone"],))
        if plan["rec_users_gone"]:
            cur.execute("DELETE FROM campaign_user_recommendations WHERE user_uuid = ANY(%s)",
                        (plan["rec_users_gone"],))
        if plan["pin_updates"]:
            cur.execute(
                "UPDATE user_wardrobe uw SET pin_code_at_first_sight = v.pin "
                "FROM unnest(%s::text[], %s::text[]) AS v(id, pin) "
                "WHERE uw.user_uuid = v.id AND uw.pin_code_at_first_sight IS DISTINCT FROM v.pin",
                ([u for u, _ in plan["pin_updates"]], [p for _, p in plan["pin_updates"]]),
            )
        if plan["requeue_events"]:
            cur.execute(
                "UPDATE garment_similarity_events SET bq_synced_at = NULL "
                "WHERE id = ANY(%s::uuid[]) AND bq_synced_at IS NOT NULL",
                (plan["requeue_events"],),
            )


def reconcile_cloud_sql(customers: dict[str, str], observed: dict[str, set], *,
                        apply: bool) -> dict[str, Any]:
    with _connect(commit=apply) as conn:
        plan = plan_cloud_sql(customers, observed, **read_cloud_sql(conn))
        if apply:
            apply_cloud_sql(conn, plan)
    return plan


def report_bigquery(res: dict[str, dict[str, Any]], *, apply: bool) -> list[str]:
    lines = ["BigQuery:"]
    for r in res.values():
        if r["error"]:
            lines.append(f"  {r['label']}: failed ({r['error']})")
            continue
        verb = r["change"] if apply else f"would be {r['change']}"
        lines.append(f"  {r['label']}: {r['rows']}" + (f" ({verb})" if r["rows"] else ""))
    return lines


def report_cloud_sql(plan: dict[str, Any], *, apply: bool) -> list[str]:
    def did(verb: str) -> str:
        return verb if apply else f"would be {verb}"

    lines = [
        "Cloud SQL:",
        f"  user_wardrobe rows of missing customers: {plan['wardrobe_rows_gone']} "
        f"({len(plan['wardrobe_users_gone'])} customers, {did('deleted')})",
        f"  campaign_user_recommendations rows of missing customers: {plan['rec_rows_gone']} ({did('deleted')})",
        f"  user_wardrobe rows with an old PIN: {plan['pin_rows']} ({did('set to the current PIN')})",
    ]
    if plan["events_tracked"]:
        oldest = plan["oldest_pending"]
        if oldest is not None and oldest.tzinfo is not None:
            oldest = oldest.astimezone(timezone.utc)
        lines.append(f"  observations waiting for BigQuery: {plan['pending_events']}"
                     + (f" (oldest {oldest:%Y-%m-%d %H:%M} UTC; suffai-similarity resends them)" if oldest else ""))
        n = len(plan["requeue_events"])
        lines.append(f"  matched photos with no BigQuery observation: {n}" + (f" ({did('queued for resend')})" if n else ""))
    else:
        lines.append("  garment_similarity_events: apply db/migrations/011_similarity_bq_sync.sql "
                     "to check BigQuery observations")
    if plan["garments_not_in_sql"]:
        lines.append(f"  observed garments missing from unique_garments: {plan['garments_not_in_sql']} "
                     "(no embedding; compare-pins skips them)")
    unlinked = plan["unlinked_garments"]
    lines.append(f"  unique_garments with no wardrobe link: {len(unlinked)} (kept)")
    for g in unlinked[:UNLINKED_SHOWN]:
        lines.append(f"    {g}")
    if len(unlinked) > UNLINKED_SHOWN:
        lines.append(f"    ... {len(unlinked) - UNLINKED_SHOWN} more")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or "")
    ap.add_argument("--dataset", default=os.getenv("BQ_DATASET", "suffai_ops"))
    ap.add_argument("--apply", action="store_true", help="Make the changes (default: dry run)")
    ap.add_argument("--skip-cloud-sql", action="store_true", help="Only reconcile BigQuery")
    args = ap.parse_args(argv)
    if not args.project:
        raise SystemExit("--project (or GOOGLE_CLOUD_PROJECT) is required")
    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    os.environ["BQ_DATASET"] = args.dataset

    try:
        n_customers = customer_count()
    except ops_records.RecordsUnavailable as exc:
        print(f"Cannot read {args.project}.{args.dataset}.customers ({exc}).")
        return 1
    if not n_customers:
        print(f"{args.project}.{args.dataset}.customers is empty; refusing to treat every row as orphaned. "
              "Check --project / --dataset.")
        return 1
    print(f"{args.project}.{args.dataset}: {n_customers} customers")

    bq = reconcile_bigquery(apply=args.apply)
    print("\n".join(report_bigquery(bq, apply=args.apply)))
    failed = sum(1 for r in bq.values() if r["error"])

    if args.skip_cloud_sql:
        print("Cloud SQL: skipped (--skip-cloud-sql)")
    else:
        try:
            customers, observed = bigquery_state()
        except ops_records.RecordsUnavailable as exc:
            print(f"Cloud SQL: skipped, BigQuery observations unreadable ({exc})")
            failed += 1
        else:
            plan = reconcile_cloud_sql(customers, observed, apply=args.apply)
            print("\n".join(report_cloud_sql(plan, apply=args.apply)))

    if not args.apply:
        print("Dry run. Re-run with --apply to make these changes.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
