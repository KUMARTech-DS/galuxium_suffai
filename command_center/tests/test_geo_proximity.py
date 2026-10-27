import copy
import json
import math
from pathlib import Path

import pytest

import aura_agent
import campaign_service as svc
import gemini_grounded as gg
import geo_analysis as ga
import geo_market as gm
import geo_proximity as gp

METRICS = json.loads((Path(__file__).resolve().parent / "fixtures" / "metrics.json").read_text(encoding="utf-8"))
INDIRANAGAR = ("560008", 12.97817, 77.64043)
KM_PER_DEG_LAT = math.pi * 6371.0088 / 180


def _store_north_of(lat, lon, km, name="Zara Test"):
    return {"name": name, "lat": lat + km / KM_PER_DEG_LAT, "lon": lon, "confidence": "high", "location_source": "model"}


def _lookup(*stores, status="ok"):
    return {"brand": "Zara", "city": "Bengaluru", "status": status, "stores": list(stores), "sources": []}


def _grounded(payload, calls=None):
    def ask(prompt):
        if calls is not None:
            calls.append(prompt)
        return gg.GroundedAnswer(text=json.dumps(payload), sources=[{"title": "Nike store list", "url": "https://example.com/s"}],
                                 search_entry_html="<div>chip</div>", model="test")
    return ask


# ---------------------------------------------------------------------------
# 4 km rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("km,zone", [(3.99, "service"), (4.0, "service"), (4.01, "whitespace"), (7.5, "whitespace")])
def test_proximity_boundary(km, zone):
    pin, lat, lon = INDIRANAGAR
    store = _store_north_of(lat, lon, km)
    assert gp.haversine_km(lat, lon, store["lat"], store["lon"]) == pytest.approx(km, abs=1e-6)
    rows = [{"pin": pin, "heuristic_decision": "Yes"}]
    gp.apply_proximity(rows, _lookup(store), METRICS["pin_geo"])
    r = rows[0]
    assert r["proximity_zone"] == zone
    if zone == "service":
        assert (r["decision"], r["decision_label"], r["mode"]) == ("No", gp.SERVICE_LABEL, "optimize")
    else:
        assert r["decision"] == "Yes" and r["mode"] == "expansion"
    assert r["expansion_decision"] == "Yes"


def test_unverified_caps_yes_and_empty_is_whitespace():
    rows = [{"pin": "560008", "heuristic_decision": "Yes"}]
    gp.apply_proximity(rows, _lookup(status="unavailable"), METRICS["pin_geo"])
    assert rows[0]["proximity_zone"] == "unverified" and rows[0]["decision"] == "Consider"
    assert rows[0]["decision_label"] == gp.UNVERIFIED_LABEL

    rows = [{"pin": "560008", "heuristic_decision": "Yes"}]
    gp.apply_proximity(rows, _lookup(status="empty"), METRICS["pin_geo"])
    assert rows[0]["proximity_zone"] == "whitespace" and rows[0]["decision"] == "Yes"


def test_gate_decision_never_raises_no():
    assert gp.gate_decision("No", "unverified")[0] == "No"
    assert gp.gate_decision("Consider", "whitespace")[0] == "Consider"
    assert gp.gate_decision("Yes", "service")[0] == "No"


# ---------------------------------------------------------------------------
# Store validation + cache
# ---------------------------------------------------------------------------


def test_validate_stores_coordinates():
    pin_geo = METRICS["pin_geo"]
    raw = [
        {"name": "Good", "pin_code": "560008", "lat": 12.979, "lon": 77.641},
        {"name": "Out of city", "address": "100 Feet Rd, Bengaluru 560008", "lat": 28.6, "lon": 77.2},
        {"name": "Far from PIN", "pin_code": "560008", "lat": 13.05, "lon": 77.64},
        {"name": "No location"},
        {"name": "Good", "pin_code": "560008", "lat": 12.979, "lon": 77.641},
        {"lat": 12.97, "lon": 77.6},
    ]
    out = {s["name"]: s for s in gp.validate_stores(raw, pin_geo=pin_geo, city="Bengaluru")}
    assert set(out) == {"Good", "Out of city", "Far from PIN"}
    assert out["Good"]["confidence"] == "high"
    assert out["Out of city"]["location_source"] == "pin_centroid" and out["Out of city"]["confidence"] == "low"
    assert out["Out of city"]["lat"] == pytest.approx(pin_geo["560008"]["centroid_lat"])
    assert out["Far from PIN"]["confidence"] == "low"


def test_store_lookup_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(gg, "_override", _grounded({"stores": [{"name": "Zara Indiranagar", "pin_code": "560008",
                                                                  "lat": 12.979, "lon": 77.641}]}, calls))
    first = gp.locate_brand_stores("Zara", pin_geo=METRICS["pin_geo"])
    second = gp.locate_brand_stores("zara", pin_geo=METRICS["pin_geo"])
    assert first["status"] == "ok" and len(first["stores"]) == 1 and not first["cached"]
    assert second["cached"] is True and len(calls) == 1
    gp.locate_brand_stores("Zara", pin_geo=METRICS["pin_geo"], refresh=True)
    assert len(calls) == 2


def test_store_lookup_failure_is_unavailable():
    res = gp.locate_brand_stores("Zara", pin_geo=METRICS["pin_geo"])
    assert res["status"] == "unavailable" and res["stores"] == []
    assert "offline" not in json.dumps(res)
    assert gp.locate_brand_stores(None, pin_geo=METRICS["pin_geo"])["status"] == "unavailable"


def test_parse_json_tolerates_prose():
    assert gg.parse_json('Here you go:\n```json\n{"stores": []}\n```') == {"stores": []}
    with pytest.raises(ValueError):
        gg.parse_json("no data")


# ---------------------------------------------------------------------------
# Analysis + routing guide
# ---------------------------------------------------------------------------


def test_decision_requires_high_value_demand():
    assert ga.decision_from_score(0.9, 5, 200, 0.6, 5, hv_demand=0) == "Consider"
    assert ga.decision_from_score(0.9, 5, 200, 0.6, 5, hv_demand=2) == "Yes"
    assert ga.decision_from_score(0.9, 5, 200, 0.6, 5) == "Yes"


def test_analyze_applies_rule_and_hv_ignores_rfm_filter():
    pin, lat, lon = INDIRANAGAR
    a = ga.analyze(METRICS, role="admin", session_brand=None, analysis_brand="Zara", rfms=["At Risk"],
                   stores_lookup=_lookup(_store_north_of(lat, lon, 1.0)))
    row = next(r for r in a["pins"] if r["pin"] == pin)
    assert row["hv_demand"] > 0
    assert row["proximity_zone"] == "service" and row["decision"] == "No"
    assert a["proximity"]["service_pins"] >= 1 and a["proximity"]["threshold_km"] == 4.0
    for r in a["pins"]:
        assert r["proximity_zone"] in {"service", "whitespace"}
        if r["proximity_zone"] == "service":
            assert r["decision"] == "No"


def test_routing_guide_min_group_size():
    pins = ["560008"]
    small = ga.routing_guide(METRICS, pins=pins, role="admin", session_brand=None, analysis_brand="Zara", min_customers=2)
    assert [p["persona"] for p in small["personas"]] == ["Other segments"]
    assert small["personas"][0]["customers"] == 3 and small["suppressed_customers"] == 0

    strict = ga.routing_guide(METRICS, pins=pins, role="admin", session_brand=None, analysis_brand="Zara", min_customers=5)
    assert strict["personas"] == [] and strict["suppressed_customers"] == 3

    open_ = ga.routing_guide(METRICS, pins=pins, role="admin", session_brand=None, analysis_brand="Zara", min_customers=1)
    assert open_["personas"][0]["high_value"] is True
    assert open_["assortment"]["size_curve"] and open_["assortment"]["commodity"]
    blob = json.dumps(open_)
    for key in ("user_uuid", "phone", "email", "centroid_lat"):
        assert key not in blob


def test_routing_guide_brand_has_no_competitor_names():
    g = ga.routing_guide(METRICS, pins=list(METRICS["pin_geo"]), role="brand", session_brand="Zara", min_customers=1)
    blob = json.dumps(g)
    for competitor in ["Nike", "H&M", "Levis", "Uniqlo", "Fabindia", "Snitch"]:
        assert competitor not in blob


# ---------------------------------------------------------------------------
# External market enrichment
# ---------------------------------------------------------------------------

MARKET = {
    "footfall": {"level": "high", "drivers": ["Metro station", "Nike flagship"], "notes": ""},
    "rent": {"inr_per_sqft_month_low": 180, "inr_per_sqft_month_high": 320, "rental_yield_pct": None, "notes": ""},
    "competition": {"level": "high", "apparel_store_count_estimate": 40, "malls_nearby": ["Phoenix"],
                    "competitors": ["Nike", "Uniqlo", "Zara"], "notes": "Nike and Uniqlo are nearby"},
    "viability": "unfavourable",
    "summary": "Strong footfall but Nike dominates.",
}


def test_enrichment_refused_within_4km(monkeypatch):
    pin, lat, lon = INDIRANAGAR
    with pytest.raises(gm.EnrichmentRefused):
        gm.enrich_whitespace(pin, metrics=METRICS, brand="Zara", role="admin",
                             stores_lookup=_lookup(_store_north_of(lat, lon, 2.0)))


def test_enrichment_masks_competitors_for_brand_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(gg, "_override", _grounded(MARKET, calls))
    pin, lat, lon = INDIRANAGAR
    lookup = _lookup(_store_north_of(lat, lon, 9.0))
    res = gm.enrich_whitespace(pin, metrics=METRICS, brand="Zara", role="brand", stores_lookup=lookup)
    assert res["status"] == "ok" and res["proximity_zone"] == "whitespace"
    blob = json.dumps(res)
    assert "Nike" not in blob and "Uniqlo" not in blob
    assert "competitors" not in res["market"]["competition"]
    assert res["market"]["competition"]["competitor_count"] == 2
    assert "Never name any apparel brand" in calls[0]

    again = gm.enrich_whitespace(pin, metrics=METRICS, brand="Zara", role="brand", stores_lookup=lookup)
    assert again["cached"] is True and len(calls) == 1

    admin = gm.enrich_whitespace(pin, metrics=METRICS, brand="Zara", role="admin", stores_lookup=lookup)
    assert admin["market"]["competition"]["competitors"] == ["Nike", "Uniqlo", "Zara"]
    assert len(calls) == 2
    assert gm.cached(pin, brand="Zara", role="brand")["status"] == "ok"


def test_enrichment_only_lowers():
    ok = {"status": "ok", "market": {"viability": "unfavourable"}}
    assert gm.adjust_decision("Yes", ok)[0] == "Consider"
    assert gm.adjust_decision("Consider", ok)[0] == "No"
    good = {"status": "ok", "market": {"viability": "supportive"}}
    assert gm.adjust_decision("Consider", good)[0] == "Consider"
    assert gm.adjust_decision("No", good)[0] == "No"
    assert gm.adjust_decision("Yes", {"status": "unavailable"})[0] == "Yes"


# ---------------------------------------------------------------------------
# Server enforcement in /v1/geo/brief
# ---------------------------------------------------------------------------


@pytest.fixture
def brief_env(monkeypatch):
    import chatbot
    import main

    monkeypatch.setenv("ENABLE_VERTEX", "true")
    monkeypatch.setattr(svc, "load_metrics", lambda fresh=False: copy.deepcopy(METRICS))
    model_reply = {"decision": "Yes", "confidence": "high", "explanation": "Strong demand.", "brief": "Open.",
                   "key_factors": ["demand"]}
    seen = {}

    def fake_vertex(message, payload, request_id, **kw):
        seen["payload"] = payload
        return json.dumps(model_reply)

    monkeypatch.setattr(chatbot, "call_vertex_chat", fake_vertex)
    return main, seen


def _brief_body(main):
    """What POST /v1/geo/brief hands to geo_brief_core for PIN 560008 (server scores: Zara, expansion Yes)."""
    import geo_service

    return main.GeoBriefRequest(**geo_service.brief_input(
        role="admin", session_brand=None, pin="560008", filters={}, analysis_brand="Zara"))


def test_brief_forces_no_within_4km(brief_env, monkeypatch):
    main, seen = brief_env
    pin, lat, lon = INDIRANAGAR
    store = _store_north_of(lat, lon, 0.5, name="Zara Indiranagar")
    monkeypatch.setattr(gg, "_override", _grounded({"stores": [{**store, "pin_code": "560008"}]}))
    out = main.geo_brief_core(_brief_body(main), {"role": "admin"})
    assert out["model_decision"] == "Yes"
    assert out["decision"] == "No" and out["decision_label"] == gp.SERVICE_LABEL and out["mode"] == "optimize"
    assert out["proximity"]["zone"] == "service" and out["proximity"]["nearest_store_km"] <= 4
    assert any("4 km" in s["step"] for s in out["decision_logic"])
    assert out["stock_mix"] and out["stock_mix"]["store"] == "Zara Indiranagar"
    assert seen["payload"]["focus_pin"]["binding_decision"] == "No"
    assert seen["payload"]["focus_pin"]["hv_demand"] == 2  # Champions/Loyal from analyze


def test_brief_caps_when_unverified(brief_env):
    main, _ = brief_env
    out = main.geo_brief_core(_brief_body(main), {"role": "admin"})
    assert out["decision"] == "Consider" and out["proximity"]["zone"] == "unverified"


def test_brief_whitespace_keeps_yes_and_market_lowers(brief_env, monkeypatch):
    main, _ = brief_env
    pin, lat, lon = INDIRANAGAR
    monkeypatch.setattr(gg, "_override", _grounded({"stores": [{**_store_north_of(lat, lon, 8.0), "pin_code": ""}]}))
    out = main.geo_brief_core(_brief_body(main), {"role": "admin"})
    assert out["proximity"]["zone"] == "whitespace" and out["decision"] == "Yes"

    monkeypatch.setattr(gg, "_override", _grounded(MARKET))
    gm.enrich_whitespace(pin, metrics=METRICS, brand="Zara", role="admin", stores_lookup=gp.locate_brand_stores(
        "Zara", pin_geo=METRICS["pin_geo"]))
    out = main.geo_brief_core(_brief_body(main), {"role": "admin"})
    assert out["decision"] == "Consider" and out["external_market"]["viability"] == "unfavourable"


def test_brief_fallback_also_enforced(brief_env, monkeypatch):
    main, _ = brief_env
    monkeypatch.setenv("ENABLE_VERTEX", "false")
    pin, lat, lon = INDIRANAGAR
    monkeypatch.setattr(gg, "_override", _grounded({"stores": [{**_store_north_of(lat, lon, 1.0), "pin_code": "560008"}]}))
    out = main.geo_brief_core(_brief_body(main), {"role": "admin"})
    assert out["fallback"] is True and out["decision"] == "No" and out["decision_label"] == gp.SERVICE_LABEL


# ---------------------------------------------------------------------------
# AURA
# ---------------------------------------------------------------------------


def _ctx(role="brand", brand="Zara", token="tok-geo"):
    sess = {"token": token, "role": role, "brand_name": brand, "allowed_tabs": ["geo", "ads"]}
    return aura_agent.ToolContext(
        sess=sess, role=role, brand=brand, token=token, ui={}, filter_ctx={},
        state=aura_agent._state_for(token), request_id="t-geo",
    )


def test_aura_routing_guide_and_analyze(monkeypatch):
    monkeypatch.setattr(svc, "load_metrics", lambda fresh=False: copy.deepcopy(METRICS))
    pin, lat, lon = INDIRANAGAR
    monkeypatch.setattr(gg, "_override", _grounded({"stores": [{**_store_north_of(lat, lon, 0.5), "pin_code": "560008"}]}))
    guide = aura_agent.execute_tool("stock_routing_guide", {"pin": pin}, _ctx())
    assert guide.result["ok"] is True and guide.result["mode"] == "optimize"
    assert guide.result["assortment"]["commodity"]
    far = aura_agent.execute_tool("stock_routing_guide", {"pin": "560066"}, _ctx())
    assert far.result["ok"] is False and far.result["proximity_zone"] == "whitespace"

    out = aura_agent.execute_tool("analyze_geo", {"use_current_filters": False}, _ctx())
    row = next(r for r in out.result["pins"] if r["pin"] == pin)
    assert row["proximity_zone"] == "service" and row["decision"] == "No"
    blob = json.dumps(out.result)
    for competitor in ["Nike", "H&M", "Levis", "Uniqlo", "Fabindia", "Snitch"]:
        assert competitor not in blob
