"""migrate_bq_retail.sql, create_bq_tables.sql and load_login_creds_bq.sh agree on the login table."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "command_center" / "scripts"


def _columns(sql: str) -> list[str]:
    body = re.search(r"CREATE TABLE IF NOT EXISTS [`\\\w.${}]*login_cred_db[`\\]*\s*\((.*?)\)\s*;", sql, re.S).group(1)
    return [line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith("--")]


def test_login_table_has_location_code_everywhere():
    ddl = _columns((SCRIPTS / "create_bq_tables.sql").read_text(encoding="utf-8"))
    migration_sql = (SCRIPTS / "migrate_bq_retail.sql").read_text(encoding="utf-8")
    loader = (SCRIPTS / "load_login_creds_bq.sh").read_text(encoding="utf-8")
    assert ddl[-1] == "location_code"
    assert _columns(migration_sql) == ddl == _columns(loader)
    assert "ADD COLUMN IF NOT EXISTS location_code STRING" in migration_sql
    schema = re.search(r"^\s*(username:STRING,\S+)$", loader, re.M).group(1)
    assert [c.split(":")[0] for c in schema.split(",")] == ddl
    assert "--allow_jagged_rows" in loader and "REPLACE_STAFF_LOGINS" in loader


def test_bootstrap_runs_the_retail_migration():
    boot = (ROOT / "deploy" / "bootstrap_new_project.sh").read_text(encoding="utf-8")
    assert "migrate_bq_retail.sh" in boot
    assert (ROOT / "db" / "migrations" / "012_retail_ops.sql").exists()
