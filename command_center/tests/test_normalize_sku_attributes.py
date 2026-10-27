"""normalize_sku_attributes.py re-maps stored SKU attributes with the worker's customer-garment mapping."""

import json

import normalize_sku_attributes as nsa

RAW = {"commodity_type": "t_shirt", "primary_color": "navy_blue", "pattern": "solid", "material_type": "cotton",
       "size_normalized": "m", "sleeve_type": "long_sleeve", "collar_type": "unknown"}


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))

    def fetchall(self):
        return self.conn.rows


class _Conn:
    def __init__(self, rows):
        self.rows, self.sql, self.committed, self.closed = rows, [], False, False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_mapped_columns_match_customer_garment_spelling():
    cols = nsa.mapped_columns(json.dumps(RAW))
    assert cols == {"color": "Navy Blue", "pattern": "Solid", "material": "Cotton", "size": "M",
                    "commodity": "T Shirt", "sleeve_type": "Long Sleeve", "collar_type": None}
    assert nsa.mapped_columns(None) is None and nsa.mapped_columns({}) is None


def test_dry_run_plans_only_changed_rows(monkeypatch):
    done = nsa.mapped_columns(RAW)
    rows = [("s1", RAW, "navy_blue", "solid", "cotton", "m", "t_shirt", "long_sleeve", None),
            ("s2", RAW, *done.values())]
    conn = _Conn(rows)
    monkeypatch.setattr(nsa, "_pg_connect", lambda: conn)
    changes = nsa.run(apply=False)
    assert [s for s, _ in changes] == ["s1"]
    assert not conn.committed and conn.closed
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.sql)


def test_apply_updates_columns(monkeypatch):
    conn = _Conn([("s1", RAW, "navy_blue", "solid", "cotton", "m", "t_shirt", "long_sleeve", None)])
    monkeypatch.setattr(nsa, "_pg_connect", lambda: conn)
    nsa.run(apply=True)
    sql, params = conn.sql[-1]
    assert sql.startswith("UPDATE brand_skus SET color = %s")
    assert params == ("Navy Blue", "Solid", "Cotton", "M", "T Shirt", "Long Sleeve", None, "s1")
    assert conn.committed
