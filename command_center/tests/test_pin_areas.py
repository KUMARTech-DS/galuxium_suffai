"""PIN area profiles: grounded lookup, validation, BigQuery pin_areas, metrics and the Geo endpoint."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import aggregate_metrics as agg
import gemini_grounded as grounded
import pin_areas

KORAMANGALA = {"found": True, "pin_code": "560034", "area_label": "Koramangala", "city": "Bengaluru",
               "population": 51000, "population_year": 2011, "area_sq_km": 6.4, "lat": 12.9352, "lon": 77.6245}


class FakeTable:
    """pin_areas in memory: answers the SELECTs and MERGE that pin_areas.py issues."""

    def __init__(self, rows=None):
        self.rows = {r["pin_code"]: dict(r) for r in rows or []}
        self.merges = 0

    def __call__(self, sql, params):
        p = {n: v for n, _t, v in params}
        if sql.startswith("MERGE"):
            self.merges += 1
            row = {k: v for k, v in p.items()}
            row["fetched_at"] = datetime.now(timezone.utc)
            self.rows[p["pin_code"]] = row
            return []
        if "WHERE pin_code = @pin" in sql:
            r = self.rows.get(p["pin"])
            return [dict(r)] if r else []
        if "customers" in sql:
            return [{"pin": "560034"}, {"pin": "560008"}, {"pin": "56A01"}]
        return [dict(r) for r in self.rows.values()]


def _answer(data, sources=None):
    return grounded.GroundedAnswer(text=json.dumps(data), sources=sources or [{"title": "Census", "url": "https://x"}],
                                   search_entry_html="<div>chip</div>", model="gemini-test")


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(pin_areas, "_runner", t)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    return t


def _ground(monkeypatch, data, calls=None):
    def fake(prompt):
        if calls is not None:
            calls.append(prompt)
        if isinstance(data, Exception):
            raise data
        return _answer(data)

    monkeypatch.setattr(grounded, "_override", fake)


def test_normalize_ok_computes_buffers_from_density():
    row = pin_areas.normalize(KORAMANGALA, pin="560034", city="Bengaluru")
    assert row["status"] == "ok" and row["area_label"] == "Koramangala"
    density = 51000 / 6.4
    assert row["pop_buffer_2km"] == round(density * 3.141592653589793 * 4)
    assert row["pop_buffer_1km"] < row["pop_buffer_2km"] < row["pop_buffer_4km"]
    assert (row["centroid_lat"], row["centroid_lon"]) == (12.9352, 77.6245)


@pytest.mark.parametrize(
    "change,status",
    [
        ({"lat": 19.07, "lon": 72.87}, "out_of_city"),           # Mumbai
        ({"found": False}, "not_found"),
        ({"pin_code": "560035"}, "not_found"),                    # answered a different PIN
        ({"lat": None}, "not_found"),
        ({"area_label": ""}, "not_found"),
    ],
)
def test_normalize_rejects(change, status):
    row = pin_areas.normalize({**KORAMANGALA, **change}, pin="560034", city="Bengaluru")
    assert row["status"] == status
    assert row["centroid_lat"] is None and row["population_pin"] is None and row["pop_buffer_4km"] is None


def test_normalize_drops_implausible_figures():
    row = pin_areas.normalize({**KORAMANGALA, "population": 12, "area_sq_km": -3, "population_year": 1700},
                              pin="560034", city="Bengaluru")
    assert row["status"] == "ok"
    assert row["population_pin"] is None and row["area_sq_km"] is None and row["population_year"] is None
    assert row["pop_buffer_2km"] is None


def test_lookup_saves_then_serves_from_cache(monkeypatch, table):
    calls = []
    _ground(monkeypatch, KORAMANGALA, calls)
    first = pin_areas.lookup("560034")
    assert first["status"] == "ok" and first["cached"] is False and first["saved"] is True
    assert first["sources"] == [{"title": "Census", "url": "https://x"}] and first["search_entry_html"]
    assert table.merges == 1 and json.loads(table.rows["560034"]["sources"])[0]["url"] == "https://x"
    pin_areas.clear_cache()
    again = pin_areas.lookup("560034")
    assert again["cached"] is True and again["area_label"] == "Koramangala"
    assert len(calls) == 1 and table.merges == 1


def test_lookup_redoes_an_expired_profile(monkeypatch, table):
    _ground(monkeypatch, KORAMANGALA)
    old = datetime.now(timezone.utc) - timedelta(days=pin_areas.cache_days() + 1)
    table.rows["560034"] = {**pin_areas.normalize(KORAMANGALA, pin="560034", city="Bengaluru"),
                            "area_label": "Old name", "sources": "[]", "fetched_at": old}
    res = pin_areas.lookup("560034")
    assert res["cached"] is False and res["area_label"] == "Koramangala"


def test_lookup_unavailable_saves_nothing(monkeypatch, table):
    _ground(monkeypatch, grounded.GroundingUnavailable("down"))
    res = pin_areas.lookup("560034")
    assert res["status"] == "unavailable" and table.merges == 0 and "down" not in json.dumps(res)


def test_failed_refresh_keeps_the_saved_profile(monkeypatch, table):
    _ground(monkeypatch, KORAMANGALA)
    pin_areas.lookup("560034")
    _ground(monkeypatch, grounded.GroundingUnavailable("down"))
    res = pin_areas.lookup("560034", refresh=True)
    assert res["status"] == "ok" and res["stale"] is True and res["area_label"] == "Koramangala"


def test_lookup_validates_pin_and_rate_limits(monkeypatch, table):
    import aura_guard

    with pytest.raises(ValueError):
        pin_areas.lookup("56003")
    _ground(monkeypatch, KORAMANGALA)
    monkeypatch.setattr(pin_areas, "_limiter", aura_guard.RateLimiter(per_minute=1))
    pin_areas.lookup("560034", refresh=True, rate_key="u")
    with pytest.raises(pin_areas.RateLimited):
        pin_areas.lookup("560034", refresh=True, rate_key="u")
    assert pin_areas.lookup("560034", rate_key="u")["cached"] is True  # cached answers are not limited


def test_require_ok(monkeypatch, table):
    _ground(monkeypatch, {**KORAMANGALA, "lat": 19.07, "lon": 72.87})
    with pytest.raises(pin_areas.AreaRejected, match="outside Bengaluru"):
        pin_areas.require_ok("560034")
    with pytest.raises(pin_areas.AreaRejected, match="6 digits"):
        pin_areas.require_ok("12345")
    pin_areas.clear_cache()
    table.rows.clear()
    _ground(monkeypatch, grounded.GroundingUnavailable("down"))
    with pytest.raises(pin_areas.AreaRejected, match="could not be verified"):
        pin_areas.require_ok("560034")
    _ground(monkeypatch, KORAMANGALA)
    assert pin_areas.require_ok("560034")["centroid_lat"] == 12.9352


def test_load_all_without_the_table_is_empty(monkeypatch):
    def missing(sql, params):
        raise pin_areas.AreaUnavailable("pin_areas query failed: NotFound: 404 Not found: Table p:suffai_ops.pin_areas")

    monkeypatch.setattr(pin_areas, "_runner", missing)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    assert pin_areas.load_all() == {}


def test_metrics_pin_geo_comes_from_pin_areas_only_for_customer_pins():
    customers = [
        {"_id": "c1", "pin_code": "560034", "age": 30, "gender": "Female", "rfm_segment": "Loyal", "lat": 12.0, "lon": 77.0},
        {"_id": "c2", "pin_code": "560008", "age": 41, "gender": "Male", "rfm_segment": "New", "lat": 12.97, "lon": 77.64},
    ]
    areas = {
        "560034": {**pin_areas.normalize(KORAMANGALA, pin="560034", city="Bengaluru"), "fetched_at": "2026-10-01T00:00:00+00:00"},
        "560102": {**pin_areas.normalize({**KORAMANGALA, "pin_code": "560102"}, pin="560102", city="Bengaluru")},
        "560066": {"pin_code": "560066", "status": "out_of_city", "area_label": "Somewhere"},
    }
    geo = agg.build(customers, [], [], areas)["pin_geo"]
    assert set(geo) == {"560034", "560008"}  # 560102 has no customers, so it is not a dashboard PIN
    k = geo["560034"]
    assert (k["area_label"], k["centroid_lat"], k["centroid_source"], k["area_status"]) == \
        ("Koramangala", 12.9352, "pin_areas", "ok")
    assert k["population_pin"] == 51000 and k["pop_buffer_2km"] > 0
    i = geo["560008"]
    assert (i["area_label"], i["centroid_lat"], i["centroid_source"], i["area_status"], i["population_pin"]) == \
        ("560008", 12.97, "customers", "missing", None)


def test_metrics_degrade_when_pin_areas_is_unreadable(monkeypatch):
    import main

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("USE_BQ", "true")
    monkeypatch.setattr(agg, "load_from_bq", lambda *a, **k: (
        [{"_id": "c1", "pin_code": "560034", "age": 30, "gender": "Female", "rfm_segment": "Loyal"}], [], []))
    monkeypatch.setattr(main, "_count_extraction_jobs", lambda: None)
    payload = main.compute_metrics()
    assert payload["pin_geo"]["560034"]["area_status"] == "missing"


SESSIONS = {"adm": {"role": "admin", "username": "ops"}, "brd": {"role": "brand", "username": "b", "brand_name": "Zara"}}
METRICS = {"pin_geo": {"560034": {"area_label": "560034"}}, "garment_cells": [], "filters": {}}


@pytest.fixture
def client(monkeypatch, table):
    from fastapi.testclient import TestClient

    import auth_acl
    import main

    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    monkeypatch.setattr(main, "compute_metrics", lambda: copy.deepcopy(METRICS))
    main._clear_metrics_cache()
    return TestClient(main.app)


def test_area_profile_endpoint(monkeypatch, client):
    import main

    _ground(monkeypatch, KORAMANGALA)
    post = lambda tok, **body: client.post("/v1/geo/area-profile", json=body,  # noqa: E731
                                           headers={"X-Session-Token": tok})
    assert post("brd", pin="560034", refresh=True).status_code == 403
    assert post("brd", pin="560099").status_code == 404
    assert post("adm", pin="5600").status_code == 400
    main.cached_metrics()
    r = post("brd", pin="560034")
    assert r.status_code == 200 and r.json()["area_label"] == "Koramangala" and r.json()["status"] == "ok"
    assert main._metrics_cache["payload"] is None  # a saved profile shows on every tab at once
    r = post("adm", pin="560034", refresh=True)
    assert r.status_code == 200 and r.json()["cached"] is False


def test_backfill_plan(table):
    import backfill_pin_areas as bf

    stored = {"560034": {"status": "ok"}}
    todo, done, bad = bf.plan(bf.customer_pins(), stored, refresh=False)
    assert (todo, done, bad) == (["560008"], ["560034"], ["56A01"])
    assert bf.plan(["560034"], stored, refresh=True)[0] == ["560034"]
