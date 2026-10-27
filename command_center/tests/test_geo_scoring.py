"""One Geo store scorer: the map (/v1/geo/scores), the brief, the market check and AURA give the same answer."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth_acl
import aura_agent
import campaign_service as svc
import chatbot
import gemini_grounded as gg
import geo_analysis as ga
import geo_proximity as gp
import geo_service
import main

METRICS = json.loads((Path(__file__).resolve().parent / "fixtures" / "metrics.json").read_text(encoding="utf-8"))
COMPETITORS = ["Nike", "H&M", "Levis", "Uniqlo", "Fabindia", "Snitch"]
SESSIONS = {
    "adm": {"role": "admin", "username": "ops-admin", "token": "adm"},
    "brd": {"role": "brand", "username": "zara-user", "brand_name": "Zara", "token": "brd"},
}
ADM = {"X-Session-Token": "adm"}
_WF = METRICS["pin_geo"]["560066"]
# One Zara store 1 km north of Whitefield: 560066 is served by it, every other fixture PIN is more than 10 km away.
STORES = {"stores": [{"name": "Zara Whitefield", "pin_code": "560066",
                      "lat": _WF["centroid_lat"] + 0.009, "lon": _WF["centroid_lon"]}]}
EXPECTED = {"560008": "Yes", "560034": "Consider", "560066": "No", "560102": "No", "560041": "No"}
MARKET = {
    "footfall": {"level": "high", "drivers": ["Metro station"], "notes": ""},
    "rent": {"inr_per_sqft_month_low": 180, "inr_per_sqft_month_high": 320, "rental_yield_pct": None, "notes": ""},
    "competition": {"level": "high", "apparel_store_count_estimate": 40, "malls_nearby": [], "competitors": [], "notes": ""},
    "viability": "unfavourable",
    "summary": "Busy, but crowded.",
}


def _answer(payload, calls=None):
    def ask(prompt):
        if calls is not None:
            calls.append(prompt)
        return gg.GroundedAnswer(text=json.dumps(payload), sources=[], model="test")
    return ask


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(svc, "load_metrics", lambda fresh=False: copy.deepcopy(METRICS))
    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    monkeypatch.setenv("ENABLE_VERTEX", "true")
    seen = []

    def contrarian_vertex(message, payload, request_id, **kw):
        """Gemini always disagrees with the rule, so a matching decision proves the rule is binding."""
        seen.append(payload)
        binding = (payload.get("focus_pin") or {}).get("binding_decision")
        return json.dumps({"decision": "Yes" if binding == "No" else "No", "confidence": "high",
                           "explanation": "Model view.", "brief": "Brief.", "key_factors": []})

    monkeypatch.setattr(chatbot, "call_vertex_chat", contrarian_vertex)
    calls = []
    monkeypatch.setattr(gg, "_override", _answer(STORES, calls))
    return TestClient(main.app, raise_server_exceptions=False), seen, calls


def _post(client, path, body, token="adm"):
    r = client.post(path, json=body, headers={"X-Session-Token": token})
    assert r.status_code == 200, r.text
    return r.json()


def _scores(client, filters=None, brand="Zara", token="adm"):
    return _post(client, "/v1/geo/scores", {"filters": filters or {}, "analysis_brand": brand}, token)


def _ctx(token="tok-geo-admin"):
    sess = {"token": token, "role": "admin", "allowed_tabs": ["geo", "ads"]}
    return aura_agent.ToolContext(sess=sess, role="admin", brand="", token=token, ui={}, filter_ctx={},
                                  state=aura_agent._state_for(token), request_id="t-scoring")


def _json(obj):
    return json.loads(json.dumps(obj))


def _many_pins():
    """Fixture plus nine copies of Indiranagar, so Jayanagar (560041) ranks below the top 8."""
    m = copy.deepcopy(METRICS)
    for i in range(9):
        pin = f"5609{i:02d}"
        m["pin_geo"][pin] = {**METRICS["pin_geo"]["560008"], "area_label": f"Copy {i}",
                             "centroid_lat": METRICS["pin_geo"]["560008"]["centroid_lat"] + 0.001 * (i + 1)}
        for key in ("customer_cells", "garment_cells"):
            m[key] += [{**c, "pin_code": pin} for c in METRICS[key] if str(c.get("pin_code")) == "560008"]
    return m


# ---------------------------------------------------------------------------
# The map's rows are the analysis rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filters", [
    {},
    {"age": ["25-34"], "gender": ["F"]},
    {"pins": ["560008", "560034", "560066"], "rfm": ["Champions", "Loyal"]},
])
def test_scores_route_rows_are_analyze_rows(env, filters):
    client, _, _ = env
    out = _scores(client, filters)
    want = ga.analyze(METRICS, role="admin", session_brand=None, pins=filters.get("pins"), ages=filters.get("age"),
                      genders=filters.get("gender"), rfms=filters.get("rfm"), analysis_brand="Zara", top_n=None,
                      stores_lookup=gp.cached_brand_stores("Zara"))
    assert out["pins"] == _json(want["pins"])
    assert out["analysis_brand"] == "Zara" and out["threshold_km"] == gp.PROXIMITY_KM


def test_scores_route_brand_login_is_masked(env):
    client, _, _ = env
    r = client.post("/v1/geo/scores", json={"filters": {}, "analysis_brand": "Nike"}, headers={"X-Session-Token": "brd"})
    assert r.status_code == 200
    out = r.json()
    masked = auth_acl.mask_metrics_for_brand(copy.deepcopy(METRICS), "Zara")
    want = ga.analyze(masked, role="brand", session_brand="Zara", top_n=None, stores_lookup=gp.cached_brand_stores("Zara"))
    assert out["analysis_brand"] == "Zara" and out["pins"] == _json(want["pins"])
    assert {b["value"] for b in out["top_brands"]} <= {"Zara", "Competitors"}
    for competitor in COMPETITORS:
        assert competitor not in r.text


def test_admin_default_brand_follows_the_selected_pins(env):
    client, _, _ = env
    assert _scores(client, brand=None)["analysis_brand"] == "Zara"
    assert _scores(client, {"pins": ["560066"]}, brand=None)["analysis_brand"] == "Nike"


def test_scores_read_the_store_cache_only(env):
    client, _, calls = env
    assert gp.cached_brand_stores("Zara")["status"] == "unavailable"
    assert gp.cached_brand_stores(None)["status"] == "unavailable"
    before = _scores(client)
    assert calls == [] and {r["proximity_zone"] for r in before["pins"]} == {"unverified"}

    assert client.get("/v1/geo/stores", params={"brand": "Zara"}, headers=ADM).json()["status"] == "ok"
    assert len(calls) == 1
    zones = {r["pin"]: r["proximity_zone"] for r in _scores(client)["pins"]}
    assert zones["560066"] == "service" and zones["560008"] == "whitespace"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Map, brief and AURA agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pin", sorted(EXPECTED))
def test_map_brief_and_aura_agree(env, pin):
    client, seen, _ = env
    client.get("/v1/geo/stores", params={"brand": "Zara"}, headers=ADM)  # the page looks the stores up first
    row = next(r for r in _scores(client)["pins"] if r["pin"] == pin)
    brief = _post(client, "/v1/geo/brief", {"pin": pin, "filters": {}, "analysis_brand": "Zara"})
    aura_brief = aura_agent.execute_tool("run_geo_brief", {"pin": pin, "use_current_filters": False, "analysis_brand": "Zara"}, _ctx())
    aura_rows = aura_agent.execute_tool("analyze_geo", {"use_current_filters": False, "apply_to_dashboard": False,
                                                        "analysis_brand": "Zara"}, _ctx()).result["pins"]
    summary_rows = aura_agent.execute_tool("get_metrics_summary", {}, _ctx()).result["top_pins"]

    assert row["decision"] == EXPECTED[pin]
    assert brief["decision"] == row["decision"] == aura_brief.result["decision"]
    assert brief["model_decision"] != brief["decision"]
    assert seen[-1]["focus_pin"]["pin"] == pin
    # analyze_geo lists the top 8 and the metrics summary the top 5 (Zara is the admin default brand here)
    for listed in (aura_rows, summary_rows):
        r = next((x for x in listed if x["pin"] == pin), None)
        if r:
            assert (r["score"], r["decision"]) == (row["score"], row["decision"])


def test_brief_ignores_numbers_sent_by_the_browser(env):
    client, seen, _ = env
    clean = _post(client, "/v1/geo/brief", {"pin": "560008", "filters": {}, "analysis_brand": "Zara"})
    want = seen[-1]
    spoofed = _post(client, "/v1/geo/brief", {
        "pin": "560008", "filters": {}, "analysis_brand": "Zara", "role": "admin",
        "focus_pin": {"pin": "560008", "score": 0.0, "hv_demand": 99, "brand_share": 0.01, "heuristic_decision": "No"},
        "top_pins": [{"pin": "560041", "score": 1.0, "heuristic_decision": "Yes"}],
        "kpis": {"customers": 999999},
    })
    got = seen[-1]
    assert spoofed["decision"] == clean["decision"]
    assert got["focus_pin"] == want["focus_pin"] and got["top_pins"] == want["top_pins"] and got["kpis"] == want["kpis"]
    assert got["focus_pin"]["hv_demand"] == 2 and got["kpis"]["customers"] != 999999


def test_focus_pin_outside_the_top_8_is_used(env, monkeypatch):
    client, seen, _ = env
    monkeypatch.setattr(svc, "load_metrics", lambda fresh=False: _many_pins())
    payload = geo_service.brief_input(role="admin", session_brand=None, pin="560041", filters={}, analysis_brand="Zara")
    assert len(payload["top_pins"]) == 8 and "560041" not in {r["pin"] for r in payload["top_pins"]}
    assert payload["focus_pin"]["pin"] == "560041"

    _post(client, "/v1/geo/brief", {"pin": "560041", "filters": {}, "analysis_brand": "Zara"})
    assert seen[-1]["focus_pin"]["pin"] == "560041"
    aura = aura_agent.execute_tool("run_geo_brief", {"pin": "560041", "use_current_filters": False, "analysis_brand": "Zara"}, _ctx())
    assert aura.result["focus_pin"]["pin"] == "560041"


def test_market_check_starts_from_the_server_decision(env, monkeypatch):
    client, _, _ = env
    client.get("/v1/geo/stores", params={"brand": "Zara"}, headers=ADM)
    monkeypatch.setattr(gg, "_override", _answer(MARKET))
    out = _post(client, "/v1/geo/market-enrichment",
                {"pin": "560008", "analysis_brand": "Zara", "filters": {}, "expansion_decision": "No"})
    assert out["status"] == "ok"
    assert (out["decision_before"], out["decision_after"]) == ("Yes", "Consider")


# ---------------------------------------------------------------------------
# The rule re-checks a row to the same answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filters", [{}, {"age": ["18-24", "25-34"]}, {"rfm": ["At Risk"]}])
def test_rule_rechecks_give_the_row_decision(filters):
    a = ga.analyze(METRICS, role="admin", session_brand=None, ages=filters.get("age"), rfms=filters.get("rfm"),
                   analysis_brand="Zara", top_n=None)
    for r in a["pins"]:
        assert ga.decision_for_row(r) == r["heuristic_decision"]
        focus = main._sanitize_geo_brief_payload(main.GeoBriefRequest(focus_pin=r))["focus_pin"]
        assert focus["heuristic_decision"] == r["heuristic_decision"] == ga.decision_for_row(focus)
