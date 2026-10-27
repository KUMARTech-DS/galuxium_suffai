"""Geo demand -> opening stock, campaign stock and actual POS sales, snapshot."""

import json
from datetime import timedelta

import pytest

import aura_agent
import retail_db
import retail_insights as insights
import retail_pos as pos
import retail_wms as wms
import vocab
from retail_db import RetailError
from test_retail_flow import HQ, OTHER, _stock_up, _variant, clock, shop  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _vocab(monkeypatch):
    monkeypatch.setattr(vocab, "_loader", lambda: [])
    vocab.clear_cache()
    yield
    vocab.clear_cache()


def cell(pin, rfm, names, colors, sizes):
    def items(d):
        return [{"value": k, "count": n} for k, n in d.items()]
    return {"pin_code": pin, "age_group": "25-34", "gender": "F", "rfm_segment": rfm, "brand": "Competitors",
            "garments": sum(names.values()), "names": items(names), "colors": items(colors), "sizes": items(sizes)}


METRICS = {"garment_cells": [
    cell("560038", "Champions", {"Shirt": 30, "Jacket": 5}, {"White": 25, "Navy": 5, "Black": 5}, {"M": 20, "L": 10, "S": 5}),
    cell("560038", "Hibernating", {"Shirt": 10}, {"Navy": 10}, {"S": 10}),
]}


def test_opening_plan_follows_area_demand(shop):
    _stock_up(shop)
    plan = insights.opening_plan(HQ, METRICS, pin_code="560038", units=40,
                                 source_location_id=shop["wh"]["location_id"])
    assert plan["demand"]["basis"] == "pin" and plan["store"]["code"] == "BLR01"
    assert plan["units_planned"] == 40 and plan["units_short"] == 0
    cats = {c["category"]: c for c in plan["categories"]}
    assert cats["Shirt"]["units"] > cats["Jacket"]["units"] > 0
    shirts = {(x["color"], x["size"]): x["qty"] for x in plan["lines"] if x["category"] == "Shirt"}
    assert shirts[("White", "M")] == max(shirts.values())
    assert plan["demand"]["top_commodities"][0]["value"] == "Shirt"

    near = insights.opening_plan(HQ, METRICS, pin_code="560001", units=10,
                                 source_location_id=shop["wh"]["location_id"])
    assert near["demand"]["basis"] == "district" and near["store"] is None
    none = insights.opening_plan(HQ, METRICS, pin_code="110017", units=10,
                                 source_location_id=shop["wh"]["location_id"])
    assert none["demand"]["basis"] == "none" and none["units_planned"] == 10

    tr = insights.create_opening_transfer(HQ, {
        "store_id": shop["other"]["location_id"], "source_location_id": shop["wh"]["location_id"],
        "lines": plan["lines"]})
    assert tr["source"] == "opening_plan" and tr["units_requested"] == 40 and tr["to_code"] == "BLR02"
    with pytest.raises(RetailError) as e:
        insights.create_opening_transfer(shop["wh_staff"], {
            "store_id": shop["other"]["location_id"], "source_location_id": shop["wh"]["location_id"],
            "lines": plan["lines"]})
    assert e.value.status == 403


def test_capped_allocation_moves_overflow_to_the_next_best():
    got = insights.capped_allocation({"a": 0.9, "b": 0.06, "c": 0.04}, 50, {"a": 10, "b": 100, "c": 100})
    assert got["a"] == 10 and sum(got.values()) == 50 and got["b"] > got["c"]
    assert sum(insights.capped_allocation({"a": 1.0, "b": 1.0}, 50, {"a": 3, "b": 4}).values()) == 7
    assert insights.capped_allocation({"a": 1.0}, 5, {"a": 0}) == {"a": 0}


def test_opening_plan_fills_from_other_sizes_when_the_top_sku_runs_out(shop):
    _stock_up(shop)
    plan = insights.opening_plan(HQ, METRICS, pin_code="560038", units=80,
                                 source_location_id=shop["wh"]["location_id"])
    assert all(x["qty"] <= x["available"] for x in plan["lines"])
    cats = {c["category"]: c["units"] for c in plan["categories"]}
    jackets = [x for x in plan["lines"] if x["category"] == "Jacket"]
    assert cats["Shirt"] == 70 and all(x["qty"] == x["available"] for x in jackets)
    assert plan["units_short"] == 80 - plan["units_planned"] == 10 - cats["Jacket"]
    white_m = next(x for x in plan["lines"] if (x["color"], x["size"]) == ("White", "M"))
    assert white_m["qty"] == white_m["available"]


def test_opening_plan_caps_at_warehouse_stock(shop):
    _stock_up(shop)
    plan = insights.opening_plan(HQ, METRICS, pin_code="560038", units=5000,
                                 source_location_id=shop["wh"]["location_id"])
    assert plan["units_planned"] == 120 + 10 - 8 and plan["units_short"] == 5000 - plan["units_planned"]


def test_campaign_check_counts_actual_sales_in_window(shop, clock):
    white_m, jacket_m = _stock_up(shop)
    campaign = {"campaign_id": "c-1", "pin_codes": ["560038"], "start_date": "2026-10-20", "end_date": "2026-10-26",
                "selected_sku_ids": ["s1"]}
    skus = [{"sku_uuid": "s1", "attributes": {"commodity": "Shirt", "color": "White"}},
            {"sku_uuid": "s2", "attributes": {"commodity": "Kurta", "color": "Red"}}]
    pos.campaign_lookup = lambda scope, cid: {"campaign_id": cid} if cid == "c-1" else None
    try:
        pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 2}],
                                          "payments": [{"mode": "upi", "amount": "3998"}], "campaign_id": "c-1"})
        with pytest.raises(RetailError):
            pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 1}],
                                              "payments": [{"mode": "upi", "amount": "1999"}], "campaign_id": "nope"})
    finally:
        pos.campaign_lookup = None
    out = insights.campaign_check(HQ, campaign, skus)
    assert out["label"] == "Actual (POS)"
    assert [(s["code"], s["in_campaign_pins"]) for s in out["stores"]] == [("BLR01", True), ("BLR02", False)]
    assert len(out["readiness"]) == 1 and out["readiness"][0]["matched_variants"] == 3
    assert out["readiness"][0]["store_units"] == 3
    sales = out["sales"]
    assert sales["window"] == {"from": "2026-10-20", "to": "2026-10-20", "days": 1}
    assert sales["campaign_items"]["units"] == 2 and sales["campaign_items"]["prior"]["units"] == 0
    assert sales["tagged_bills"] == {"invoices": 1, "units": 2, "net_paise": 399800}

    clock["t"] += timedelta(days=3)
    later = insights.campaign_check(HQ, campaign, skus)
    assert later["sales"]["window"]["days"] == 4
    assert insights.campaign_check(OTHER, campaign, skus)["stores"] == []


def test_snapshot_has_only_aggregates(shop):
    white_m, _ = _stock_up(shop)
    pos.create_sale(shop["cashier"], {"lines": [{"variant_id": white_m["variant_id"], "qty": 1}],
                                      "customer": {"name": "Meera", "phone": "9845012345"},
                                      "payments": [{"mode": "cash", "amount": "2000"}]})
    snap = insights.snapshot(HQ)
    assert snap["stores"] == 2 and snap["warehouses"] == 1 and snap["styles"] == 2
    assert snap["net_sales_today"] == 1999.0 and snap["bills_today"] == 1
    assert snap["transfers_in_transit"] == 0
    flat = repr(snap)
    assert "Meera" not in flat and "9845012345" not in flat
    assert wms.list_transfers(HQ)["total"] == 1


def _aura(sess, args=None):
    ctx = aura_agent.ToolContext(sess=sess, role=sess["role"], brand=sess.get("brand_name") or "", token="t-retail",
                                 ui={}, filter_ctx={}, state=aura_agent._state_for("t-retail"), request_id="t1")
    return aura_agent.execute_tool("get_retail_snapshot", args or {}, ctx).result


BRAND_SESS = {"role": "brand", "username": "zara_hq", "brand_name": "Zara", "allowed_tabs": ["geo", "inventory"]}


def test_aura_retail_snapshot_is_actual_and_hides_ids(shop):
    _stock_up(shop)
    out = _aura(BRAND_SESS)
    assert out["label"] == "Actual (POS)" and out["warehouses"] == 1 and out["stores"] == 2
    assert out["billing_set_up"] is True and "configured" not in out
    assert out["by_location"] and all(set(loc) == set(aura_agent._SNAPSHOT_LOCATION_KEYS) for loc in out["by_location"])
    assert sum(loc["units"] for loc in out["by_location"]) == out["units_on_hand"]
    flat = json.dumps(out)
    assert not any(shop[k]["location_id"] in flat for k in ("wh", "blr", "other"))


def test_aura_retail_snapshot_access(shop, monkeypatch):
    cashier = {"role": "store_staff", "username": "blr_asha", "brand_name": "Zara", "location_code": "BLR01",
               "allowed_tabs": ["pos"]}
    assert _aura(cashier)["error_code"] == "not_allowed"
    assert _aura({**BRAND_SESS, "allowed_tabs": ["geo", "ads"]})["error_code"] == "not_allowed"
    admin = {"role": "admin", "username": "root", "brand_name": "", "allowed_tabs": ["geo", "inventory"]}
    missing = _aura(admin)
    assert missing["error_code"] == "invalid_input" and missing["hint"]
    assert _aura(admin, {"analysis_brand": "Zara"})["stores"] == 2
    assert _aura({**BRAND_SESS, "brand_name": "H&M"})["stores"] == 0
    monkeypatch.setattr(retail_db, "configured", lambda: False)
    down = _aura(BRAND_SESS)
    assert down["error_code"] == "unavailable" and "DATABASE_URL" not in json.dumps(down)
