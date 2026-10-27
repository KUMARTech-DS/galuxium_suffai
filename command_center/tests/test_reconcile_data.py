"""reconcile_data.py: dry run reports and changes nothing; --apply cleans orphans and requeues photos missing
from BigQuery. Garment counts per PIN are never rebuilt in Cloud SQL (BigQuery garment_items is the count)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import ops_records
import reconcile_data as rd

G1 = "11111111-1111-1111-1111-111111111111"
G2 = "22222222-2222-2222-2222-222222222222"
G3 = "33333333-3333-3333-3333-333333333333"
ORPHANS = {"orders": 2, "commodities": 5, "garment_similarity_observations": 0, "login_cred_db": 1}
T0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
E = {n: f"e000000{n}-0000-0000-0000-000000000000" for n in range(1, 10)}


def _ev(n, key, order, item, user, pending, minutes):
    return (E[n], key, order, item, user, pending, T0 + timedelta(minutes=minutes))


# (id, gcs_object_key, order_id, item_id, user_uuid, bq_synced_at IS NULL, created_at), oldest first
EVENTS = [
    _ev(6, "garments/c1_o5_1.jpg", "o5", "1", "c1", False, -60),  # superseded by the pending event 7
    _ev(8, "garments/c1_o6_1.jpg", "o6", "1", "c1", False, -50),  # older than event 9, same item
    _ev(1, "garments/c1_o1_1.jpg", "o1", "1", "c1", False, -40),  # in BigQuery
    _ev(2, "garments/c1_o2_1.jpg", None, None, "c1", False, -30),  # before migration 011: ids from the name
    _ev(3, "garments/gone_o3_1.jpg", "o3", "1", "gone", False, -20),  # customer deleted
    _ev(5, "demo/look.jpg", None, None, "c1", False, -10),  # no order or item in the name
    _ev(4, "garments/c1_o4_1.jpg", "o4", "1", "c1", True, 0),  # waiting for BigQuery
    _ev(7, "garments/c1_o5_1.jpg", "o5", "1", "c1", True, 5),
    _ev(9, "garments/c1_o6_1.jpg", "o6", "1", "c1", False, 10),
]


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        if sql.startswith("SELECT user_uuid, garment_id"):
            self.rows = [("c1", G1, "560001"), ("c1", G2, "560001"), ("gone", G2, "560002")]
        elif "to_regclass" in sql:
            self.rows = [(True,)]
        elif "FROM campaign_user_recommendations GROUP BY" in sql:
            self.rows = [("c1", 2), ("gone", 3)]
        elif sql.startswith("SELECT garment_id::text FROM unique_garments"):
            self.rows = [(G1,), (G2,), (G3,)]
        elif "information_schema.columns" in sql:
            self.rows = [(self.conn.event_columns,)]
        elif "FROM garment_similarity_events" in sql:
            self.rows = list(EVENTS)
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class Conn:
    def __init__(self):
        self.sql = []
        self.done = None
        self.event_columns = 3

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.done = "commit"

    def rollback(self):
        self.done = self.done or "rollback"

    def close(self):
        pass

    def writes(self):
        return [s for s, _ in self.sql if s.split()[0] in ("DELETE", "UPDATE", "INSERT")]


@pytest.fixture
def env(monkeypatch):
    bq = {"sql": [], "customers": 2}

    def runner(sql, params):
        bq["sql"].append(sql)
        if sql.startswith("SELECT COUNT(*) AS n FROM") and " x WHERE " not in sql:
            return [{"n": bq["customers"]}]
        if sql.startswith("SELECT COUNT(*) AS n FROM"):
            table = sql.split("`")[1].split(".")[-1]
            return [{"n": ORPHANS[table]}]
        if sql.startswith("SELECT CAST(_id AS STRING) AS id, TRIM"):
            return [{"id": "c1", "pin": "560034"}, {"id": "c2", "pin": "bad"}]
        if sql.startswith("SELECT DISTINCT LOWER(x.garment_id)"):
            return [{"garment_id": G1}, {"garment_id": "9" * 8}]
        if sql.startswith("SELECT DISTINCT order_id, item_id"):
            return [{"order_id": "o1", "item_id": "1"}]
        return []

    conn = Conn()
    monkeypatch.setattr(ops_records, "_runner", runner)
    monkeypatch.setattr(rd, "_pg_connect", lambda: conn)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("BQ_DATASET", "suffai_ops")  # main() writes both; restored after the test
    return bq, conn


def _dml(bq):
    return [s for s in bq["sql"] if s.startswith(("DELETE", "UPDATE"))]


def test_dry_run_reports_counts_and_changes_nothing(env, capsys):
    bq, conn = env
    assert rd.main(["--project", "proj", "--dataset", "ops"]) == 0
    out = capsys.readouterr().out
    assert "proj.ops: 2 customers" in out
    assert "orders whose customer is missing: 2 (would be deleted)" in out
    assert "garment items whose order or customer is missing: 5 (would be deleted)" in out
    assert "similarity observations of missing customers: 0\n" in out
    assert "active customer logins whose customer is missing: 1 (would be deactivated)" in out
    assert "user_wardrobe rows of missing customers: 1 (1 customers, would be deleted)" in out
    assert "campaign_user_recommendations rows of missing customers: 3 (would be deleted)" in out
    assert "user_wardrobe rows with an old PIN: 2 (would be set to the current PIN)" in out
    assert "observations waiting for BigQuery: 2 (oldest 2026-10-20 06:00 UTC; suffai-similarity resends them)" in out
    assert "matched photos with no BigQuery observation: 2 (would be queued for resend)" in out
    assert "observed garments missing from unique_garments: 1 (no embedding; compare-pins skips them)" in out
    assert "unique_garments with no wardrobe link: 1 (kept)" in out and G3 in out
    assert "garment_pin_counts" not in out and not any("garment_pin_counts" in s for s, _ in conn.sql)
    assert "Dry run." in out and "c1" not in out and "gone" not in out and E[2] not in out
    assert _dml(bq) == [] and conn.writes() == [] and conn.done == "rollback"


def test_apply_cleans_bigquery_and_requeues_missing_observations(env, capsys):
    bq, conn = env
    assert rd.main(["--project", "proj", "--apply"]) == 0
    dml = _dml(bq)
    assert [s.split("`")[1].split(".")[-1] for s in dml] == ["orders", "commodities", "login_cred_db"]
    assert dml[0].startswith("DELETE FROM `proj.suffai_ops.orders` x WHERE NOT EXISTS")
    assert dml[2].startswith("UPDATE `proj.suffai_ops.login_cred_db` x SET active = FALSE WHERE x.role = 'customer'")
    writes = {s.split(" (")[0].split(" WHERE")[0].split(" USING")[0].split(" FROM unnest")[0]: p for s, p in conn.sql
              if s.split()[0] in ("DELETE", "UPDATE", "INSERT")}
    assert writes == {
        "DELETE FROM user_wardrobe": (["gone"],),
        "DELETE FROM campaign_user_recommendations": (["gone"],),
        "UPDATE user_wardrobe uw SET pin_code_at_first_sight = v.pin": (["c1"], ["560034"]),
        "UPDATE garment_similarity_events SET bq_synced_at = NULL": ([E[2], E[9]],),
    }
    assert conn.done == "commit"
    out = capsys.readouterr().out
    assert "orders whose customer is missing: 2 (deleted)" in out and "Dry run." not in out
    assert "matched photos with no BigQuery observation: 2 (queued for resend)" in out


def test_without_migration_011_events_are_not_checked(env, capsys):
    bq, conn = env
    conn.event_columns = 1
    assert rd.main(["--project", "proj", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "apply db/migrations/011_similarity_bq_sync.sql to check BigQuery observations" in out
    assert "waiting for BigQuery" not in out
    assert not any("FROM garment_similarity_events" in s or s.startswith("UPDATE garment_similarity_events")
                   for s, _ in conn.sql)


def test_empty_customers_table_refuses_to_run(env, capsys):
    bq, conn = env
    bq["customers"] = 0
    assert rd.main(["--project", "proj", "--apply"]) == 1
    assert "refusing" in capsys.readouterr().out
    assert len(bq["sql"]) == 1 and conn.sql == []


def test_skip_cloud_sql_and_bigquery_failure_exit_code(env, monkeypatch, capsys):
    bq, conn = env
    assert rd.main(["--project", "proj", "--skip-cloud-sql"]) == 0
    assert conn.sql == [] and "Cloud SQL: skipped" in capsys.readouterr().out

    def down(sql, params):
        if "x WHERE" in sql:
            raise ops_records.RecordsUnavailable("BigQuery write failed: NotFound")
        return [{"n": 2}]

    monkeypatch.setattr(ops_records, "_runner", down)
    assert rd.main(["--project", "proj", "--skip-cloud-sql"]) == 1
    assert "failed (BigQuery write failed: NotFound)" in capsys.readouterr().out


def test_plan_requeues_only_synced_items_of_live_customers():
    plan = rd.plan_cloud_sql(
        {"c1": "560034", "c2": "56003"},
        {"garments": {G1}, "items": {("o1", "1")}},
        wardrobe=[("c1", G1, "560034"), ("c2", G1, "560001")],
        rec_users={},
        garments={G1},
        events=EVENTS,
    )
    assert plan["requeue_events"] == [E[2], E[9]]
    assert plan["pending_events"] == 2 and plan["oldest_pending"] == T0
    assert plan["pin_updates"] == [] and plan["wardrobe_users_gone"] == []
    assert plan["garments_not_in_sql"] == 0 and plan["unlinked_garments"] == []
    assert not any(k.startswith("count_") for k in plan)
