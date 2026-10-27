from pathlib import Path

from suffai.ingest import load_source_collections, normalize_all, referential_integrity_report

DATA = Path(__file__).resolve().parents[1] / "data_offline"


def test_normalize_preserves_line_ids_and_quantity():
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    assert len(norm["orders"]) == 19908
    assert len(norm["order_commodity_lines"]) == 39769
    qty = sum(l["quantity"] for l in norm["order_commodity_lines"])
    assert qty == 259377
    # Sample line keeps nested _id
    sample = next(l for l in norm["order_commodity_lines"] if l.get("line_id"))
    assert sample["line_id"]
    assert sample["catalog_commodity_id"]


def test_customer_join_gate():
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    report = referential_integrity_report(norm)
    assert report["gates"]["correct_customer_join_majority"]
    assert report["gates"]["wrong_user_id_join_near_zero"]
    assert report["joins"]["orders_customer_id_to_customers_user_id_WRONG"]["matched"] == 0


def test_quantity_line_is_not_garment_instance():
    """A qty=4 shirt line must not imply 4 colored garments."""
    raw = load_source_collections(DATA)
    norm = normalize_all(raw)
    four = next(l for l in norm["order_commodity_lines"] if l.get("quantity") == 4)
    assert "primary_color" not in four
    assert "brand_id" not in four
