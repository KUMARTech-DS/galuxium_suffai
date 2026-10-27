"""Customer photo path of the extraction worker: filename parsing, BigQuery item rows, order linking."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker import bq_items, customer_photos
from worker.photo_key import PhotoKey, needs_extraction, parse_photo_key, short

ROOT = Path(__file__).resolve().parents[1]
CID, OID = "65a1b2c3d4e5f60718293a4b", "75a1b2c3d4e5f60718293a4c"


@pytest.fixture
def bq(monkeypatch):
    """Fake BigQuery: records every (sql, params); orders holds order_id -> customer_id, customers the ids."""
    calls = []
    orders = {}
    customers = {CID}

    def runner(sql, params):
        calls.append((sql, params))
        p = dict((n, v) for n, _, v in params)
        if sql.lstrip().upper().startswith("SELECT") and "order_id" in p:
            oid, cid = p["order_id"], p["customer_id"]
            return [{"order_id": oid, "customer_id": cid}] if orders.get(oid) == cid and cid in customers else []
        return []

    monkeypatch.setattr(bq_items, "_runner", runner)
    monkeypatch.setattr(bq_items.settings, "PROJECT_ID", "proj")
    monkeypatch.setattr(bq_items.settings, "USE_BQ", True)
    monkeypatch.setattr(bq_items.settings, "BQ_DATASET", "suffai_ops")
    return SimpleNamespace(calls=calls, orders=orders, customers=customers)


@pytest.mark.parametrize("key, expected", [
    (f"garments/{CID}_{OID}_1.jpg", PhotoKey(CID, OID, "1")),
    (f"{CID}_{OID}_item-7.JPEG", PhotoKey(CID, OID, "item-7")),
    (f"uploads/2026/{CID}_{OID}_a_b.png", PhotoKey(CID, OID, "a_b")),
])
def test_parse_photo_key(key, expected):
    assert parse_photo_key(key) == expected


@pytest.mark.parametrize("key", [
    f"brands/{CID}_{OID}_1.jpg",       # brand SKU images go through /v1/extract
    f"demo/{CID}_{OID}_1.jpg",
    f"garments/{CID}_{OID}_1.webp",    # not a supported photo type
    f"garments/{CID}_{OID}.jpg",       # no item id
    "garments/USER/560001/test.jpg",   # legacy path layout
    f"garments/{'x' * 65}_{OID}_1.jpg",
    "",
    None,
])
def test_parse_photo_key_rejects(key):
    assert parse_photo_key(key) is None


def test_needs_extraction_skips_brand_and_unknown_objects():
    assert needs_extraction({"gcs_object_key": f"garments/{CID}_{OID}_1.jpg"})
    assert needs_extraction({"gcs_object_key": "demo/shirt.jpg"})
    assert needs_extraction({"gcs_object_key": "anything.jpg", "image_id": "abc"})
    assert not needs_extraction({"gcs_object_key": "brands/acme/sku.jpg"})
    assert not needs_extraction({"gcs_object_key": "garments/random.jpg"})


def test_short_id_is_log_safe():
    assert short(CID) == CID[:6] + "…"
    assert short("1") == "1"


def _result(**over):
    base = {
        "commodity_type": "shirt", "display_name": "Oxford Shirt", "primary_color": "navy_blue",
        "pattern": "solid", "material_type": "cotton", "brand_name": "unknown", "size_normalized": "m",
        "sleeve_type": "long_sleeve", "collar_type": "button_down", "garment_department": "Men",
        "model_version": "gemini-test", "notes": "",
        "attribute_evidence": {"commodity_type": {"confidence": 0.92}, "primary_color": {"confidence": 0.81}},
    }
    return {**base, **over}


def test_item_row_maps_dashboard_columns():
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    row = bq_items.item_row(PhotoKey(CID, OID, "3"), object_key="garments/x.jpg", result=_result(), status="COMPLETED", now=now)
    assert {c for c, _ in bq_items.ITEM_COLUMNS} == set(row)
    assert row["commodity_id"] == "3" and row["item_id"] == "3"
    assert row["name"] == "Oxford Shirt"
    assert row["color"] == "Navy Blue" and row["sleeve_type"] == "Long Sleeve"
    assert row["brand"] == "unknown"
    assert row["size"] == "M"
    assert row["gender"] == "Male"
    assert row["confidence_score"] == 0.81
    assert row["image_status"] == "extracted" and row["source"] == "pipeline"
    assert row["extracted_at"] is now
    assert row["reason"] == "Extracted from the garment photo"


def test_item_attributes_are_the_item_row_attributes():
    attrs = bq_items.item_attributes(_result(display_name="", commodity_type="t_shirt"))
    row = bq_items.item_row(PhotoKey(CID, OID, "3"), object_key="k", status="COMPLETED",
                            result=_result(display_name="", commodity_type="t_shirt"))
    assert attrs == {k: row[k] for k in attrs}
    assert attrs["name"] == "T Shirt" and attrs["color"] == "Navy Blue" and attrs["sleeve_type"] == "Long Sleeve"
    assert set(attrs) == {"name", "color", "brand", "pattern", "size", "material", "sleeve_type", "collar_type", "gender"}


def _stub_worker_deps(monkeypatch):
    """worker.main imports psycopg, Vertex AI and Cloud Storage, which are not installed for unit tests."""
    import sys
    from types import ModuleType

    def mod(name, **attrs):
        m = ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    for name in ("psycopg", "vertexai", "google.cloud"):
        if name not in sys.modules:
            try:
                __import__(name)
                continue
            except ImportError:
                pass
            stub = object
            if name == "psycopg":
                mod("psycopg.rows", dict_row=stub)
                mod("psycopg.types", json=mod("psycopg.types.json", Json=stub))
                mod("psycopg")
            elif name == "vertexai":
                mod("vertexai.generative_models", GenerationConfig=stub, GenerativeModel=stub, Part=stub)
                mod("vertexai", init=lambda **k: None)
            else:
                mod("google.cloud", storage=mod("google.cloud.storage", Client=stub))
    monkeypatch.delitem(sys.modules, "worker.main", raising=False)


def test_extract_response_carries_item_attributes(monkeypatch):
    _stub_worker_deps(monkeypatch)
    from worker import main as wm

    monkeypatch.setattr(wm.settings, "ENABLE_VERTEX", True)
    monkeypatch.setattr(wm.settings, "DATABASE_URL", "")
    monkeypatch.setattr(wm, "read_image", lambda key, bucket=None: SimpleNamespace(
        gs_uri=f"gs://b/{key}", content_type="image/jpeg", size_bytes=3))
    monkeypatch.setattr(wm, "extract_attributes", lambda image: _result(review_status="COMPLETED"))
    out = wm.run_extraction({"gcs_object_key": "brands/acme/c1/sku.jpg", "target_type": "garment"})
    assert out["item_attributes"] == bq_items.item_attributes(_result())
    assert out["item_attributes"]["color"] == "Navy Blue"


def test_item_columns_exist_in_commodities_ddl(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "command_center" / "scripts"))
    import load_csv_bq

    ddl = load_csv_bq.DDL_PATH.read_text(encoding="utf-8")
    declared = {n: t for n, t, _ in load_csv_bq.ddl_columns("commodities", ddl)}
    for col, typ in bq_items.ITEM_COLUMNS:
        assert declared.get(col) == typ, col


def test_item_row_stub_model_has_no_confidence_and_unknown_gender():
    row = bq_items.item_row(PhotoKey(CID, OID, "3"), object_key="k", status="NEEDS_REVIEW",
                            result=_result(model_version="stub-v1", garment_department="unknown", display_name=""))
    assert row["confidence_score"] is None
    assert row["gender"] == "unknown"
    assert row["name"] == "Shirt"
    assert row["image_status"] == "needs_review"


def test_merge_is_keyed_on_order_and_item(bq):
    row = bq_items.item_row(PhotoKey(CID, OID, "3"), object_key="k", result=_result(), status="COMPLETED")
    bq_items.upsert_item(row)
    sql, params = bq.calls[-1]
    assert sql.startswith("MERGE `proj.suffai_ops.commodities`")
    assert "ON t.order_id = s.order_id AND t.item_id = s.item_id" in sql
    assert "UPDATE SET customer_id = @customer_id" in sql
    assert "order_id = @order_id," not in sql.split("WHEN NOT MATCHED")[0]  # keys are never updated
    by_name = {n: (t, v) for n, t, v in params}
    assert by_name["order_id"] == ("STRING", OID)
    assert by_name["confidence_score"][0] == "FLOAT64"
    assert by_name["extracted_at"][0] == "TIMESTAMP"
    assert len(params) == len(bq_items.ITEM_COLUMNS)


def _process(photo, *, extract):
    image = SimpleNamespace(object_key="garments/" + "_".join(photo) + ".jpg", gs_uri="gs://b/x.jpg")
    return customer_photos.process(
        photo, object_key=image.object_key, bucket="b",
        read_image=lambda key, bucket=None: image,
        extract=extract, abstain=lambda reason: {"review_status": "NEEDS_REVIEW", "model_version": "stub-v1"},
    )


def test_unknown_order_is_unlinked_without_gemini(bq):
    def boom(_image):
        raise AssertionError("Gemini must not run for an order that does not exist")

    out = _process(PhotoKey(CID, OID, "1"), extract=boom)
    assert out["status"] == "unlinked"
    assert not [c for c in bq.calls if c[0].startswith("MERGE")]


def test_order_of_another_customer_is_unlinked(bq):
    bq.orders[OID] = "someone-else"
    out = _process(PhotoKey(CID, OID, "1"), extract=lambda img: _result(review_status="COMPLETED"))
    assert out["status"] == "unlinked"


def test_order_of_missing_customer_is_unlinked(bq):
    bq.orders[OID] = CID
    bq.customers.clear()
    out = _process(PhotoKey(CID, OID, "1"), extract=lambda img: _result(review_status="COMPLETED"))
    assert out["status"] == "unlinked"
    select = next(c for c in bq.calls if c[0].startswith("SELECT"))
    assert "JOIN `proj.suffai_ops.customers` c" in select[0]


def test_linked_photo_writes_one_item(bq):
    bq.orders[OID] = CID
    out = _process(PhotoKey(CID, OID, "1"), extract=lambda img: {**_result(), "review_status": "COMPLETED", "raw_model_json": "{}"})
    assert out["status"] == "COMPLETED"
    assert out["persist"] == {"bigquery": "commodities", "image_status": "extracted", "brand": "unknown"}
    assert "raw_model_json" not in out["result"]
    merges = [c for c in bq.calls if c[0].startswith("MERGE")]
    assert len(merges) == 1


def test_photo_supersedes_the_orders_csv_items_first(bq):
    bq.orders[OID] = CID
    _process(PhotoKey(CID, OID, "1"), extract=lambda img: {**_result(), "review_status": "COMPLETED"})
    writes = [c for c in bq.calls if c[0].startswith(("UPDATE", "MERGE"))]
    assert [s.split()[0] for s, _ in writes] == ["UPDATE", "MERGE"]
    sql, params = writes[0]
    assert sql.startswith("UPDATE `proj.suffai_ops.commodities` SET image_status = 'superseded'")
    assert "IFNULL(source, 'legacy') = 'legacy'" in sql and params == [("order_id", "STRING", OID)]


def test_bigquery_disabled_ignores_photo(bq, monkeypatch):
    monkeypatch.setattr(bq_items.settings, "USE_BQ", False)
    out = _process(PhotoKey(CID, OID, "1"), extract=lambda img: _result())
    assert out["status"] == "ignored"
    assert bq.calls == []
