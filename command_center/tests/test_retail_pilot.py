"""AURA Stock Pilot: restock, rebalance, reorder, slow and stuck stock, wardrobe fit, staff scope, apply."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import aura_agent
import auth_acl
import main
import retail_api
import retail_db as db
import retail_pilot as pilot
import retail_pos as pos
import retail_stock as stock
import retail_wms as wms
import vocab
from retail_db import RetailError
from test_retail_api import SESSIONS, call, ok
from test_retail_flow import HQ, OTHER, _stock_up, _variant, clock, shop  # noqa: F401  (fixtures)
from test_retail_insights import cell


@pytest.fixture(autouse=True)
def _vocab(monkeypatch):
    monkeypatch.setattr(vocab, "_loader", lambda: [])
    vocab.clear_cache()
    yield
    vocab.clear_cache()


# Around BLR01 (560038) shoppers mostly wear navy shirts in M; BLR02 (560034) falls back to the same district.
WARDROBES = {"garment_cells": [
    cell("560038", "Hibernating", {"Shirt": 40, "Jacket": 4}, {"Navy": 30, "White": 10, "Black": 4},
         {"M": 30, "L": 10, "S": 4}),
]}


def _ship(shop, items, to="blr"):
    tr = wms.create_transfer(shop["wh_staff"], {"to_location_id": shop[to]["location_id"], "lines": items})
    wms.dispatch_transfer(shop["wh_staff"], tr["transfer_id"], {})
    return wms.receive_transfer(HQ, tr["transfer_id"], {})


def _sell(shop, variant, qty):
    return pos.create_sale(shop["cashier"], {"lines": [{"variant_id": variant["variant_id"], "qty": qty}],
                                             "payments": [{"mode": "cash", "amount": "50000"}]})


def _kind(plan, kind):
    return [a for a in plan["actions"] if a["kind"] == kind]


def test_restock_from_sales_and_reorder_when_the_brand_runs_low(shop):
    white_m, jacket_m = _stock_up(shop)
    _ship(shop, [{"variant_id": jacket_m["variant_id"], "qty": 3}])  # the warehouse has no Black M jackets left
    _sell(shop, white_m, 4)
    _sell(shop, jacket_m, 5)
    plan = pilot.build(HQ, None)
    assert plan["label"] == "Actual (POS)" and plan["lookback_days"] == 28

    [restock] = _kind(plan, "restock")
    assert (restock["from"]["code"], restock["to"]["code"], restock["severity"]) == ("WH1", "BLR01", "medium")
    [line] = restock["lines"]
    assert (line["sku"], line["qty"], line["on_hand"], line["cover_days"]) == (white_m["sku"], 2, 1, 7.0)
    assert line["daily_sales"] == round(4 / 28, 2) and line["source_units"] == 14
    assert restock["impact_paise"] == 2 * 199900 and restock["can_apply"]

    [reorder] = _kind(plan, "reorder")
    assert plan["actions"][0] is reorder and reorder["severity"] == "high"
    assert reorder["supplier"]["name"] == "Tiruppur Knits" and reorder["to"]["code"] == "WH1"
    [jl] = reorder["lines"]
    assert (jl["sku"], jl["qty"], jl["cover_days"], jl["short"], jl["unit_cost"]) == (jacket_m["sku"], 9, 0.0, 4, 2000.0)
    assert any("already short" in w for w in reorder["why"])

    k = plan["kpis"]
    assert (k["stockouts"], k["at_risk"], k["units_to_move"], k["units_to_order"]) == (1, 2, 2, 9)
    assert plan["brief"]["headline"] == "Move 2 units to stores and reorder 9 from suppliers."
    assert all(f["fit"] is None for f in plan["stores"])

    po = pilot.apply(HQ, None, reorder["id"])
    assert (po["type"], po["units"], po["supplier"]) == ("purchase_order", 9, "Tiruppur Knits")
    full = wms.get_po(HQ, po["po_id"])
    assert full["status"] == "draft" and full["lines"][0]["unit_cost"] == 2000.0
    assert full["note"].startswith("AURA Stock Pilot")
    assert not _kind(pilot.build(HQ, None), "reorder")  # the open PO covers it now
    assert pilot.build(OTHER, None)["actions"] == []


def test_staff_see_their_own_moves_and_apply_drafts_a_transfer(shop):
    white_m, jacket_m = _stock_up(shop)
    _sell(shop, white_m, 4)
    _sell(shop, jacket_m, 2)
    [restock] = _kind(pilot.build(shop["cashier"], None), "restock")
    assert restock["can_apply"] is False and restock["to"]["code"] == "BLR01"
    with pytest.raises(RetailError) as e:
        pilot.apply(shop["cashier"], None, restock["id"])
    assert e.value.status == 403

    act = next(a for a in pilot.build(shop["wh_staff"], None)["actions"] if a["id"] == restock["id"])
    assert act["can_apply"] and act["severity"] == "high" and act["units"] == 4
    assert "out of stock" in act["why"][0]
    with pytest.raises(RetailError) as e:
        pilot.apply(shop["wh_staff"], None, act["id"], {"lines": [{"variant_id": jacket_m["variant_id"], "qty": 4}]})
    assert e.value.status == 409 and "Only 3" in str(e.value)
    with pytest.raises(RetailError, match="isn't part"):
        pilot.apply(shop["wh_staff"], None, act["id"], {"lines": [{"variant_id": "nope", "qty": 1}]})
    with pytest.raises(RetailError, match="All quantities are 0"):
        pilot.apply(shop["wh_staff"], None, act["id"], {"lines": [{"variant_id": jacket_m["variant_id"], "qty": 0}]})

    made = pilot.apply(shop["wh_staff"], None, act["id"], {"lines": [
        {"variant_id": jacket_m["variant_id"], "qty": 3}, {"variant_id": white_m["variant_id"], "qty": 2}]})
    tr = wms.get_transfer(HQ, made["transfer_id"])
    assert (tr["source"], tr["status"], tr["units_requested"], made["number"]) == ("aura", "draft", 5, tr["transfer_number"])
    with pytest.raises(RetailError) as e:
        pilot.apply(shop["wh_staff"], None, act["id"])
    assert e.value.status == 409  # the draft covers the need, so the suggestion is gone


def test_rebalances_from_a_store_that_does_not_sell_it_and_flags_the_rest(shop, clock):
    white_m, _ = _stock_up(shop)
    _ship(shop, [{"variant_id": white_m["variant_id"], "qty": 14}], to="other")
    clock["t"] += timedelta(days=22)
    _sell(shop, white_m, 5)
    plan = pilot.build(HQ, None)

    [move] = _kind(plan, "rebalance")
    assert (move["from"]["code"], move["to"]["code"], move["units"], move["severity"]) == ("BLR02", "BLR01", 4, "medium")
    assert move["can_apply"] and move["lines"][0]["source_units"] == 14
    [slow] = _kind(plan, "slow")
    assert slow["at"]["code"] == "BLR02" and slow["units"] == 10 and slow["lines"][0]["moving"] == 4
    assert slow["value_cost_paise"] == 10 * 60000 and slow["fix"] == {"type": "open", "tab": "inventory", "section": "stock"}
    assert "already suggested" in slow["why"][1]
    with pytest.raises(RetailError, match="needs a person"):
        pilot.apply(HQ, None, slow["id"])
    assert not _kind(pilot.build(shop["wh_staff"], None), "rebalance")  # neither end is their warehouse

    made = pilot.apply(HQ, None, move["id"])
    assert (made["from"]["code"], made["to"]["code"], made["units"]) == ("BLR02", "BLR01", 4)


def test_wardrobe_demand_seeds_gaps_and_scores_fit(shop):
    _stock_up(shop)
    plan = pilot.build(HQ, WARDROBES)
    fits = {f["location"]["code"]: f for f in plan["stores"]}
    blr, other = fits["BLR01"], fits["BLR02"]
    assert blr["basis"] == "pin" and blr["units"] == 7
    assert blr["dims"] == {"category": 81, "color": 32, "size": 68} and blr["fit"] == 60
    assert [g["label"] for g in blr["gaps"]] == ["Navy", "L", "Shirt"]
    assert blr["gaps"][0] == {"dim": "color", "label": "Navy", "demand_pct": 68, "stock_pct": 0}
    assert [x["label"] for x in blr["excess"]] == ["White", "M"]
    assert other["fit"] is None and other["basis"] == "district" and other["note"] == "No stock at this store yet."
    assert [f["location"]["code"] for f in plan["stores"]] == ["BLR01", "BLR02"]  # weakest scored fit first

    by_store = {a["to"]["code"]: a for a in _kind(plan, "restock")}
    for code in ("BLR01", "BLR02"):
        [line] = by_store[code]["lines"]
        assert (line["color"], line["size"], line["qty"], line["basis"], line["match_pct"]) == ("Navy", "M", 2, "demand", 100)
        assert by_store[code]["severity"] == "low"
    assert "PIN 560038" in by_store["BLR01"]["why"][0]
    assert "Navy is 68% of local demand but 0% of its stock" in plan["brief"]["points"][-1]

    view = pilot.agent_view(plan)
    flat = json.dumps(view)
    ids = [shop[k]["location_id"] for k in ("wh", "blr", "other")] + [x["variant_id"] for x in shop["shirt"]["variants"]]
    assert not any(i in flat for i in ids)
    assert view["actions"][0]["number"] == 1 and "can_create_draft" in view["actions"][0]
    assert pilot.describe(plan["actions"][0]).startswith(plan["actions"][0]["title"])


def test_stuck_documents_and_damaged_stock_surface(shop, clock):
    white_m, jacket_m = _stock_up(shop)
    s = shop
    road = wms.create_transfer(s["wh_staff"], {"to_location_id": s["other"]["location_id"],
                                               "lines": [{"variant_id": white_m["variant_id"], "qty": 2}]})
    wms.dispatch_transfer(s["wh_staff"], road["transfer_id"], {})
    draft = wms.create_transfer(s["wh_staff"], {"to_location_id": s["blr"]["location_id"],
                                                "lines": [{"variant_id": jacket_m["variant_id"], "qty": 1}]})
    po = wms.create_po(HQ, {"supplier_id": s["sup"]["supplier_id"], "location_id": s["wh"]["location_id"],
                            "expected_date": "2026-10-22", "lines": [{"variant_id": white_m["variant_id"], "qty": 10}]})
    wms.set_po_status(HQ, po["po_id"], "order")
    stock.adjust(HQ, s["wh"]["location_id"], {"reason": "damaged",
                                              "lines": [{"variant_id": white_m["variant_id"], "qty": 2, "bin_code": "A-01"}]})
    assert {a["kind"] for a in pilot.build(HQ, None)["actions"]} == {"damaged"}

    clock["t"] += timedelta(days=4)
    plan = pilot.build(HQ, None)
    kinds = {a["kind"]: a for a in plan["actions"]}
    assert set(kinds) == {"receive", "dispatch", "po_late", "damaged"}
    assert kinds["receive"]["document"] == road["transfer_number"] and kinds["receive"]["at"]["code"] == "BLR02"
    assert kinds["receive"]["fix"] == {"type": "open", "tab": "inventory", "section": "incoming"}
    assert kinds["dispatch"]["document"] == draft["transfer_number"]
    assert kinds["po_late"]["document"] == po["po_number"] and "2 days ago" in kinds["po_late"]["summary"]
    assert kinds["damaged"]["value_cost_paise"] == 2 * 60000 and not kinds["damaged"]["can_apply"]
    assert plan["brief"]["headline"] == "Clear 3 stuck documents."
    assert {a["kind"] for a in pilot.build(s["cashier"], None)["actions"]} == {"dispatch"}
    assert {a["kind"] for a in pilot.build(s["wh_staff"], None)["actions"]} == set(kinds)


AURA_SESS = {"role": "brand", "username": "zara_hq", "brand_name": "Zara", "token": "t-pilot",
             "allowed_tabs": ["geo", "ads", "campaigns", "inventory", "warehouse", "pos"]}


def _ctx(sess=AURA_SESS, ui=None):
    return aura_agent.ToolContext(sess=sess, role=sess["role"], brand=sess.get("brand_name") or "",
                                  token=sess.get("token", "t-pilot"), ui=ui or {}, filter_ctx={},
                                  state=aura_agent._state_for(sess.get("token", "t-pilot")), request_id="t-pilot")


def _confirm(ctx, pending):
    tool, args = aura_agent._confirms.consume(ctx.token, pending["token"])
    return aura_agent._CONFIRMED[tool](ctx, args)


def test_aura_runs_the_pilot_and_drafts_only_after_confirm(shop, monkeypatch):
    monkeypatch.setattr(retail_api, "demand_metrics", lambda: WARDROBES)
    white_m, jacket_m = _stock_up(shop)
    _sell(shop, white_m, 4)
    ctx = _ctx()
    run = aura_agent.execute_tool
    out = run("get_stock_actions", {}, ctx).result
    assert out["ok"] and out["label"] == "Actual (POS)" and out["actions"][0]["number"] == 1
    flat = json.dumps(out)
    assert not any(i in flat for i in [shop[k]["location_id"] for k in ("wh", "blr", "other")] + [white_m["variant_id"]])
    assert len(ctx.state.stock_actions) == len(out["actions"])

    restock = next(a for a in out["actions"] if a["kind"] == "restock" and a["to"]["code"] == "BLR01")
    before = wms.list_transfers(HQ)["total"]
    prep = run("apply_stock_action", {"number": restock["number"]}, ctx)
    assert prep.result["needs_confirmation"] and prep.pending["label"] == "Create draft transfer"
    assert "WH1" in prep.pending["summary"] and wms.list_transfers(HQ)["total"] == before  # nothing yet
    msg, cmds = _confirm(ctx, prep.pending)
    assert msg.startswith("Created draft transfer TR") and {"type": "refresh_retail"} in cmds
    assert {"type": "open_retail", "tab": "warehouse", "section": "transfers"} in cmds
    assert wms.list_transfers(HQ)["total"] == before + 1
    assert run("apply_stock_action", {"number": 7}, ctx).result["error_code"] == "invalid_input"

    found = run("find_stock", {"query": "navy linen shirts M"}, ctx).result
    assert found["matches"] == 1 and found["skus"][0]["label"] == "Linen shirt · Navy / M"
    assert [x["code"] for x in found["skus"][0]["locations"]] == ["WH1"]
    sold = run("find_stock", {"query": white_m["sku"]}, ctx).result["skus"][0]
    assert sold["sold_28d"] == 4 and {x["code"]: x["units"] for x in sold["locations"]} == {"WH1": 14, "BLR01": 1}

    fit = run("get_assortment_fit", {"store": "blr01"}, ctx).result
    assert fit["stores"][0]["store"]["code"] == "BLR01" and fit["stores"][0]["fit"] is not None
    assert run("get_assortment_fit", {"store": "nowhere"}, ctx).result["error_code"] == "not_found"

    prep = run("draft_stock_transfer", {"from_location": "wh1", "to_location": "Koramangala",
                                        "items": [{"sku": white_m["sku"].lower(), "qty": 3}]}, ctx)
    assert prep.pending and "BLR02" in prep.pending["summary"]
    msg, _ = _confirm(ctx, prep.pending)
    assert "to BLR02" in msg
    too_many = run("draft_stock_transfer", {"from_location": "WH1", "to_location": "BLR02",
                                            "items": [{"sku": white_m["sku"], "qty": 500}]}, ctx).result
    assert too_many["error_code"] == "invalid_input" and "can send only 9" in too_many["hint"]
    assert run("draft_stock_transfer", {"from_location": "WH1", "to_location": "BLR02",
                                        "items": [{"sku": "NOPE-1", "qty": 1}]}, ctx).result["error_code"] == "invalid_input"

    view = run("open_stock_view", {"tab": "warehouse", "section": "pilot"}, ctx)
    assert view.ui_commands == [{"type": "open_retail", "tab": "warehouse", "section": "pilot"}]
    assert run("open_stock_view", {"tab": "inventory", "section": "po"}, ctx).result["error_code"] == "invalid_input"
    plan = run("plan_opening_stock", {"pin": "560034"}, ctx)
    assert plan.ui_commands == [{"type": "opening_plan", "pin_code": "560034", "area_label": ""}]


def test_aura_stock_access(shop, monkeypatch):
    monkeypatch.setattr(retail_api, "demand_metrics", lambda: None)
    _stock_up(shop)
    run = aura_agent.execute_tool
    wh = {"role": "warehouse_staff", "username": "wh_ravi", "brand_name": "Zara", "location_code": "WH1",
          "token": "t-wh", "allowed_tabs": ["warehouse", "inventory"]}
    assert run("get_stock_actions", {}, _ctx(wh)).result["error_code"] == "not_allowed"
    no_retail = {**AURA_SESS, "allowed_tabs": ["geo", "ads"]}
    assert run("get_stock_actions", {}, _ctx(no_retail)).result["error_code"] == "not_allowed"
    admin = {"role": "admin", "username": "root", "brand_name": "", "token": "t-adm",
             "allowed_tabs": ["geo", "inventory", "warehouse", "admin"]}
    assert run("get_stock_actions", {}, _ctx(admin)).result["error_code"] == "invalid_input"
    assert run("get_stock_actions", {}, _ctx(admin, {"retail": {"brand": "Zara"}})).result["kpis"]["stores"] == 2
    assert run("plan_opening_stock", {"pin": "56003"}, _ctx()).result["error_code"] == "invalid_input"


def test_stock_events_answer_without_the_model(shop, monkeypatch):
    monkeypatch.setattr(retail_api, "demand_metrics", lambda: None)
    monkeypatch.delenv("ENABLE_VERTEX", raising=False)
    white_m, jacket_m = _stock_up(shop)
    _sell(shop, jacket_m, 2)
    sess = {**AURA_SESS, "token": "t-events"}
    ctx_ui = {"active_tab": "warehouse", "retail": {"tab": "warehouse", "section": "pilot"}}

    def chat(**kw):
        return aura_agent.run_agent(aura_agent.AgentChatRequest(context=ctx_ui, **kw), sess=sess)

    brief = chat(event="stock_brief")
    assert brief["ok"] and brief["answer"].startswith("Stock Pilot (actual POS sales, last 28 days): Move 2 units")
    assert "1. Restock Indiranagar" in brief["answer"]

    act = pilot.build(HQ, None)["actions"][0]
    explained = chat(event="stock_action", action_ref=act["id"])
    assert explained["answer"].startswith("Restock Indiranagar.") and "out of stock" in explained["answer"]
    assert explained["pending_action"]["label"] == "Create draft transfer"
    done = chat(confirm_token=explained["pending_action"]["token"])
    assert done["answer"].startswith("Created draft transfer") and {"type": "refresh_retail"} in done["ui_commands"]
    gone = chat(event="stock_action", action_ref=act["id"])
    assert "changed" in gone["answer"] and gone["ui_commands"] == [{"type": "refresh_retail"}]
    bad = chat(event="stock_action", action_ref="not-hex")
    assert bad.status_code == 400

    reset = aura_agent.reset_chat(aura_agent.AgentChatRequest(context=ctx_ui), sess=sess)
    assert "You're on Warehouse" in reset["answer"] and "Stock Pilot: All clear" in reset["answer"]


def test_stock_requests_route_to_the_warehouse():
    hint = aura_agent._route_hint
    for msg in ("What should I restock today?", "Anything to reorder?", "show me slow stock at Indiranagar",
                "which SKUs are out of stock", "how's the inventory looking"):
        assert hint(msg, "geo", None)["page"] == "Warehouse", msg
    assert hint("Which stores look strongest for 560034?", "warehouse", None)["page"] == "Geo"
    assert hint("What should the store stock near 560034?", "geo", None)["page"] == "Geo"
    assert hint("reorder", "geo", ["geo", "ads"]) is None
    ui = aura_agent.chatbot.sanitize_ui_context({"active_tab": "warehouse", "retail": {
        "tab": "warehouse", "section": "pilot" * 10, "brand": "Zara", "secret": "x"}})
    assert ui["retail"] == {"tab": "warehouse", "section": ("pilot" * 10)[:20], "brand": "Zara"}


def test_inr_uses_indian_grouping():
    assert [pilot._inr(p) for p in (0, 99900, 12345600, 1234567800, -50000)] == \
        ["₹0", "₹999", "₹1,23,456", "₹1,23,45,678", "-₹500"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.use_sqlite(str(tmp_path / "pilot.db"))
    monkeypatch.setattr(auth_acl, "get_session", lambda tok: SESSIONS.get(tok))
    yield TestClient(main.app, raise_server_exceptions=False)
    db.reset()


def test_pilot_over_http(client, monkeypatch):
    def broken():
        raise RuntimeError("bq down")
    monkeypatch.setattr(retail_api, "_metrics_loader", broken)
    assert client.get("/v1/retail/pilot").status_code == 401
    assert call(client, "get", "/pilot", "cus").status_code == 403
    assert call(client, "get", "/pilot", "adm").status_code == 400
    ok(call(client, "post", "/locations", "brd", json={"kind": "warehouse", "code": "WH1", "name": "DC",
                                                         "state_code": "29", "pin_code": "562123"}))
    ok(call(client, "post", "/locations", "brd", json={"kind": "store", "code": "BLR01", "name": "Indiranagar",
                                                         "state_code": "29", "pin_code": "560038"}))
    plan = ok(call(client, "get", "/pilot", "brd"))
    assert plan["actions"] == [] and plan["brief"]["headline"].startswith("All clear")
    assert plan["brief"]["demand"] is None and plan["kpis"]["stores"] == 1
    assert ok(call(client, "get", "/pilot", "till"))["actions"] == []
    r = call(client, "post", "/pilot/actions/abc123/apply", "brd", json={})
    assert r.status_code == 409 and "changed" in r.json()["detail"]
