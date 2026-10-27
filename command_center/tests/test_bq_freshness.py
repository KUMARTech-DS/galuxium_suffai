"""External BigQuery writes (extraction, similarity, loads) clear the dashboard caches within ~30 s."""

from __future__ import annotations

import pytest

import bq_freshness as fr


@pytest.fixture
def tables(monkeypatch):
    mod = {t: 1 for t in fr.TABLES}
    reads = []

    def reader(table):
        reads.append(table)
        return mod.get(table)

    monkeypatch.setattr(fr, "_reader", reader)
    monkeypatch.setattr(fr, "_callbacks", [])
    fr.reset()
    return mod, reads


def _due():
    fr._state["checked"] = 0.0


def test_first_check_is_a_baseline_then_a_change_clears_once(tables):
    mod, _ = tables
    cleared = []
    fr.on_change(lambda: cleared.append(1))
    fr.poke()
    assert cleared == []
    mod["commodities"] = 2
    mod["garment_similarity_observations"] = 5
    _due()
    fr.poke()
    assert cleared == [1]
    _due()
    fr.poke()
    assert cleared == [1]  # nothing moved since


def test_checks_at_most_once_per_interval(tables, monkeypatch):
    _, reads = tables
    fr.poke()
    fr.poke()
    assert len(reads) == len(fr.TABLES)
    monkeypatch.setenv("BQ_FRESHNESS_SEC", "0")
    _due()
    fr.poke()
    assert len(reads) == len(fr.TABLES)  # 0 turns the check off


def test_missing_table_appearing_is_not_a_change_and_errors_never_raise(tables, monkeypatch):
    mod, _ = tables
    cleared = []
    fr.on_change(lambda: cleared.append(1))
    mod["pin_areas"] = None
    fr.poke()
    mod["pin_areas"] = 9  # first seen after the migration: becomes the baseline
    _due()
    fr.poke()
    assert cleared == []

    def down(table):
        raise RuntimeError("bq down")

    monkeypatch.setattr(fr, "_reader", down)
    _due()
    fr.poke()
    assert cleared == [] and fr._state["running"] is False


def test_external_write_refreshes_cached_metrics_and_admin(tables, monkeypatch):
    import admin_intel
    import main

    mod, _ = tables
    fr.on_change(main._after_record_write)
    loads = []

    def compute():
        loads.append(1)
        return {"n": len(loads)}

    monkeypatch.setattr(main, "compute_metrics", compute)
    monkeypatch.setattr(main, "_metrics_cache", {"fn": None, "at": 0.0, "payload": None})
    admin_intel._cache.update(at=1e18, data={"stale": True})
    assert main.cached_metrics() == {"n": 1}
    assert main.cached_metrics() == {"n": 1}
    mod["orders"] = 2
    _due()
    assert main.cached_metrics() == {"n": 2}
    assert admin_intel._cache["data"] is None
