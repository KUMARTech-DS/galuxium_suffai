from pathlib import Path

import pytest

import aggregate_metrics as agg
import load_csv_bq

SAMPLE = Path(__file__).resolve().parents[2] / "command_center_data" / "sample"
DDL = load_csv_bq.DDL_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("table", ["customers", "orders", "commodities"])
def test_sample_csvs_only_use_declared_columns(table):
    header = load_csv_bq.csv_header(SAMPLE / f"{table}.csv")
    schema, _missing = load_csv_bq.plan_load(table, header, DDL)
    assert [c[0] for c in schema] == header


def test_commodities_schema_follows_header_and_adds_missing_confidence():
    header = load_csv_bq.csv_header(SAMPLE / "commodities.csv")
    schema, missing = load_csv_bq.plan_load("commodities", header, DDL)
    assert "confidence_score" not in header
    assert ("confidence_score", "FLOAT64", False) in missing
    assert dict((n, t) for n, t, _ in schema)["reason"] == "STRING"


def test_customers_phone_number_stays_a_string():
    schema, _ = load_csv_bq.plan_load("customers", load_csv_bq.csv_header(SAMPLE / "customers.csv"), DDL)
    assert dict((n, t) for n, t, _ in schema)["number"] == "STRING"


def test_undeclared_csv_column_fails():
    with pytest.raises(SystemExit):
        load_csv_bq.plan_load("orders", ["_id", "customer_id", "surprise"], DDL)


class _Field:
    def __init__(self, name):
        self.name = name


class _FakeBQ:
    def __init__(self, columns=None, live=0):
        self.columns, self.live, self.queries = columns, live, []

    def get_table(self, table_id):
        if self.columns is None:
            raise LookupError("404 not found")
        return type("T", (), {"schema": [_Field(c) for c in self.columns]})()

    def query(self, sql):
        self.queries.append(sql)
        return type("J", (), {"result": lambda _self: [{"n": self.live}]})()


def test_truncate_guard_blocks_loss_of_admin_and_pipeline_rows():
    live = _FakeBQ(["_id", "source"], live=3)
    assert load_csv_bq.live_row_count(live, "p.d.orders") == 3
    assert "source IN ('admin', 'pipeline')" in live.queries[0]
    with pytest.raises(SystemExit, match="3 row"):
        load_csv_bq.guard_truncate(live, "p.d.orders", force=False)
    load_csv_bq.guard_truncate(live, "p.d.orders", force=True)


def test_truncate_guard_allows_first_load_and_unmigrated_tables():
    for client in (_FakeBQ(None), _FakeBQ(["_id", "name"]), _FakeBQ(["_id", "source"], live=0)):
        load_csv_bq.guard_truncate(client, "p.d.customers", force=False)
    assert _FakeBQ(["_id"]).queries == []


def test_append_inserts_only_rows_with_a_new_key():
    header = load_csv_bq.csv_header(SAMPLE / "commodities.csv")
    sql = load_csv_bq.append_sql("p.d.commodities", "p.d.commodities__csv_append", "commodities", header)
    assert sql.startswith("INSERT INTO `p.d.commodities` (`customer_id`, `order_id`, `commodity_id`,")
    assert "FROM `p.d.commodities__csv_append` s WHERE NOT EXISTS (SELECT 1 FROM `p.d.commodities` t WHERE " \
           "CAST(t.`order_id` AS STRING) = CAST(s.`order_id` AS STRING) AND " \
           "CAST(t.`commodity_id` AS STRING) = CAST(s.`commodity_id` AS STRING))" in sql
    assert sql.endswith("QUALIFY ROW_NUMBER() OVER (PARTITION BY CAST(s.`order_id` AS STRING), "
                        "CAST(s.`commodity_id` AS STRING)) = 1")
    customers = load_csv_bq.append_sql("p.d.customers", "p.d.x", "customers",
                                       load_csv_bq.csv_header(SAMPLE / "customers.csv"))
    assert "CAST(t.`_id` AS STRING) = CAST(s.`_id` AS STRING)" in customers
    with pytest.raises(SystemExit, match="_id"):
        load_csv_bq.append_sql("p.d.orders", "p.d.x", "orders", ["customer_id", "amount"])
    with pytest.raises(SystemExit, match="--append supports"):
        load_csv_bq.append_sql("p.d.login_cred_db", "p.d.x", "login_cred_db", ["username"])


def test_unified_columns_declared_for_admin_and_photo_rows():
    cols = {t: [c[0] for c in load_csv_bq.ddl_columns(t, DDL)] for t in ("customers", "orders", "commodities")}
    for t in ("customers", "orders"):
        assert {"source", "created_at"} <= set(cols[t])
    assert {"item_id", "gcs_object_key", "image_status", "source", "model_version", "extracted_at"} <= set(cols["commodities"])


def test_unscored_rows_are_not_counted_as_low_confidence():
    assert agg.confidence_score({}) is None
    assert agg.confidence_score({"confidence_score": ""}) is None
    assert agg.confidence_score({"confidence_score": "0.65"}) == 0.65
    customers = [{"_id": "c1", "pin_code": "560001", "age": 30, "gender": "F", "rfm_segment": "Loyal"}]
    commodities = [
        {"customer_id": "c1", "brand": "Zara", "name": "Shirt"},
        {"customer_id": "c1", "brand": "Zara", "name": "Shirt", "confidence_score": 0.5},
    ]
    payload = agg.build(customers, [], commodities, {})
    cells = payload["garment_cells"]
    assert sum(c["confidence_n"] for c in cells) == 1
    assert sum(c["low_confidence_n"] for c in cells) == 1
