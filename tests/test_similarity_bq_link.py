"""Similarity observations carry the customer / order / item ids so BigQuery can join them to commodities."""

import re
from pathlib import Path

from similarity.bq_sync import observation_row

ROOT = Path(__file__).resolve().parents[1]


def _columns(sql: str, table: str) -> list[str]:
    body = re.search(rf"CREATE TABLE IF NOT EXISTS `[^`]*{table}` \((.*?)\);", sql, re.S).group(1)
    return [line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith("--")]


def test_observation_row_matches_table():
    ddl = (ROOT / "similarity" / "scripts" / "create_bq_similarity_tables.sql").read_text(encoding="utf-8")
    row = observation_row(garment_id="g", user_uuid="c1", pin_code="560001", brand_name=None, outcome="user_match",
                          gcs_object_key="garments/c1_o1_3.jpg", similarity_score=0.97, order_id="o1", item_id="3")
    assert set(row) == set(_columns(ddl, "garment_similarity_observations"))
    assert (row["customer_id"], row["order_id"], row["item_id"]) == ("c1", "o1", "3")


def test_migration_adds_link_columns_and_view():
    sql = (ROOT / "command_center" / "scripts" / "migrate_bq_unified.sql").read_text(encoding="utf-8")
    for col in ("customer_id", "order_id", "item_id"):
        assert f"ADD COLUMN IF NOT EXISTS {col} STRING" in sql
    assert "VIEW" in sql and "garment_items" in sql
    assert "is_repeat" in sql
