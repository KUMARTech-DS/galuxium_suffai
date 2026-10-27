"""migrate_bq_consistency.sql creates the same tables as create_bq_tables.sql and keeps one garment_items view."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "command_center" / "scripts"


def _columns(sql: str, table: str) -> list[str]:
    body = re.search(rf"CREATE TABLE IF NOT EXISTS `?[\w.]*{table}`?\s*\((.*?)\)\s*;", sql, re.S).group(1)
    return [line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith("--")]


def _view(sql: str) -> str:
    return re.search(r"CREATE OR REPLACE VIEW `suffai_ops\.garment_items` AS(.*?);", sql, re.S).group(1)


def test_new_tables_match_create_ddl():
    migration = (SCRIPTS / "migrate_bq_consistency.sql").read_text(encoding="utf-8")
    ddl = (SCRIPTS / "create_bq_tables.sql").read_text(encoding="utf-8")
    for table in ("pin_areas", "ref_values"):
        assert _columns(migration, table) == _columns(ddl, table)
    assert {"pin_code", "area_label", "population_pin", "pop_buffer_2km", "centroid_lat", "status", "sources",
            "search_entry_html"} <= set(_columns(ddl, "pin_areas"))
    assert _columns(ddl, "ref_values") == ["kind", "canonical", "alias_key"]


def test_both_migrations_define_the_same_view_without_superseded_rows():
    a = _view((SCRIPTS / "migrate_bq_consistency.sql").read_text(encoding="utf-8"))
    b = _view((SCRIPTS / "migrate_bq_unified.sql").read_text(encoding="utf-8"))
    assert " ".join(a.split()) == " ".join(b.split())
    assert "!= 'superseded'" in a and "is_repeat" in a


def test_values_are_rewritten_with_the_shared_key():
    sql = (SCRIPTS / "migrate_bq_consistency.sql").read_text(encoding="utf-8")
    for kind, table, col in (("customer_gender", "customers", "gender"), ("brand", "commodities", "brand"),
                             ("color", "commodities", "color"), ("garment_name", "commodities", "name"),
                             ("garment_gender", "commodities", "gender")):
        stmt = re.search(rf"UPDATE `suffai_ops\.{table}` c\s+SET {col} = r\.canonical.*?;", sql, re.S)
        assert stmt and f"r.kind = '{kind}'" in stmt.group(0)
        assert f"REGEXP_REPLACE(LOWER(TRIM(c.{col})), r'[^a-z0-9]', '')" in stmt.group(0)
    # Brand logins are seeded before the data spellings so their spelling wins.
    assert sql.index("login_cred_db") < sql.index("UNION ALL SELECT 'color'")
