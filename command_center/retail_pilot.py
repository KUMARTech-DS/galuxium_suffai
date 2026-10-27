"""AURA Stock Pilot: a brand's next stock moves, worked out from its own stock, open orders and POS sales
plus what shoppers around each store already wear (SuffAI Geo garment cells, aggregated per PIN).

build() ranks actions (restock a store, move stock between stores, reorder from a supplier, clear an
overdue transfer or PO, deal with slow or damaged stock) and scores each store's wardrobe fit. Every
action carries the numbers behind it. apply() turns a reviewed action into a draft transfer or a draft
purchase order; dispatching, ordering and receiving stay with people on the Warehouse and Inventory tabs.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import retail_db as db
import retail_gst as gst
import retail_insights as insights
import retail_stock as stock
import retail_valid as v
import retail_wms as wms
import vocab
from retail_db import RetailError, Tx
from retail_scope import PERMISSIONS, Scope

LOOKBACK_DAYS = 28
LOW_COVER_DAYS = 10        # a store SKU with less cover than this is restocked ...
TARGET_COVER_DAYS = 21     # ... up to this many days of its recent sales
KEEP_COVER_DAYS = 45       # a store keeps this much cover before it lends units to another store
REORDER_COVER_DAYS = 21    # brand-wide cover (every location, transfers on the road, open POs) below this ...
ORDER_COVER_DAYS = 45      # ... is reordered up to this
SLOW_MIN_UNITS = 4         # unsold for the whole lookback with at least this many units at one store ...
SLOW_AGE_DAYS = 21         # ... and delivered at least this long ago
GAP_TOP_SHARE = 0.5        # an unstocked SKU at least this close to the store's best local-demand match
GAP_MAX_PER_STORE = 6
SEED_UNITS = 2
STALE_TRANSIT_DAYS = 3
STALE_DRAFT_DAYS = 2
FIT_GAP = 0.08
FIT_EXCESS = 0.10
MAX_ACTION_LINES = 120
HISTORY_DAYS = 365
MOVE_KINDS = ("restock", "rebalance", "reorder")
_SEVERITY = {"high": 0, "medium": 1, "low": 2}

METHOD = (
    f"Sales pace is the net units each store sold over the last {LOOKBACK_DAYS} days; returns subtract.",
    f"A store SKU is restocked below {LOW_COVER_DAYS} days of cover, up to {TARGET_COVER_DAYS} days. "
    "Par levels win wherever they are set.",
    "Warehouses supply first. A store lends units only above "
    f"{KEEP_COVER_DAYS} days of its own cover, or when they haven't sold there in {LOOKBACK_DAYS} days.",
    f"A SKU is reordered when brand-wide cover (stores, warehouses, transfers on the road, open POs) is under "
    f"{REORDER_COVER_DAYS} days, up to {ORDER_COVER_DAYS} days.",
    "Wardrobe fit compares each store's stock mix by category, colour and size with the garments SuffAI has "
    "seen around its PIN (aggregated; no customer data).",
    "Nothing moves until a person reviews and creates the draft. Dispatch, ordering and receiving stay with your team.",
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _since_iso(days: int) -> str:
    return (db.now() - timedelta(days=days)).astimezone(timezone.utc).isoformat()


def _load(t: Tx, b: str) -> dict[str, Any]:
    locations = t.all("SELECT location_id, code, name, kind, city, pin_code FROM retail_locations "
                      "WHERE brand_key = %s AND active = TRUE ORDER BY kind DESC, code", (b,))
    variants = {r["variant_id"]: r for r in t.all(
        "SELECT vv.variant_id, vv.sku, vv.color, vv.size, vv.mrp_paise, vv.avg_cost_paise, p.name, p.category, "
        "p.style_code FROM retail_variants vv JOIN retail_products p ON p.product_id = vv.product_id "
        "WHERE vv.brand_key = %s AND vv.active = TRUE AND p.active = TRUE", (b,))}
    on: dict[str, dict[str, int]] = {}
    damaged: dict[str, dict[str, int]] = {}
    for r in t.all("SELECT location_id, bin_code, variant_id, on_hand FROM retail_stock WHERE brand_key = %s "
                   "AND on_hand > 0", (b,)):
        dst = damaged if r["bin_code"] == v.DAMAGED_BIN else on
        m = dst.setdefault(r["location_id"], {})
        m[r["variant_id"]] = m.get(r["variant_id"], 0) + int(r["on_hand"])

    since = (date.fromisoformat(db.today_ist()) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    sold: dict[str, dict[str, int]] = {}
    for r in t.all("SELECT i.location_id, il.variant_id, SUM(CASE WHEN i.kind = 'invoice' THEN il.qty ELSE -il.qty END) "
                   "AS units FROM retail_invoice_lines il JOIN retail_invoices i ON i.invoice_id = il.invoice_id "
                   "WHERE i.brand_key = %s AND i.business_date > %s GROUP BY i.location_id, il.variant_id", (b, since)):
        n = int(r["units"] or 0)
        if n > 0:
            sold.setdefault(r["location_id"], {})[r["variant_id"]] = n

    transfers: dict[str, dict[str, Any]] = {}
    for r in t.all("SELECT tr.transfer_id, tr.transfer_number, tr.from_location_id, tr.to_location_id, tr.status, "
                   "tr.source, tr.created_at, tr.dispatched_at, tl.variant_id, tl.qty_requested, tl.qty_sent "
                   "FROM retail_transfers tr JOIN retail_transfer_lines tl ON tl.transfer_id = tr.transfer_id "
                   "WHERE tr.brand_key = %s AND tr.status IN ('draft', 'in_transit')", (b,)):
        tr = transfers.setdefault(r["transfer_id"], {k: r[k] for k in (
            "transfer_id", "transfer_number", "from_location_id", "to_location_id", "status", "source",
            "created_at", "dispatched_at")} | {"lines": []})
        tr["lines"].append({"variant_id": r["variant_id"],
                            "qty": int(r["qty_requested"] if r["status"] == "draft" else r["qty_sent"])})

    purchase_orders: dict[str, dict[str, Any]] = {}
    for r in t.all("SELECT po.po_id, po.po_number, po.status, po.location_id, po.supplier_id, po.expected_date, "
                   "pl.variant_id, pl.qty_ordered, pl.qty_received FROM retail_purchase_orders po "
                   "JOIN retail_po_lines pl ON pl.po_id = po.po_id "
                   "WHERE po.brand_key = %s AND po.status IN ('draft', 'ordered', 'partial')", (b,)):
        po = purchase_orders.setdefault(r["po_id"], {k: r[k] for k in (
            "po_id", "po_number", "status", "location_id", "supplier_id", "expected_date")} | {"lines": []})
        po["lines"].append({"variant_id": r["variant_id"],
                            "open": max(0, int(r["qty_ordered"]) - int(r["qty_received"]))})

    cutoff = _since_iso(HISTORY_DAYS)
    last_buy = {r["variant_id"]: r for r in t.all(
        "SELECT pl.variant_id, po.supplier_id, po.location_id, pl.unit_cost_paise FROM retail_po_lines pl "
        "JOIN retail_purchase_orders po ON po.po_id = pl.po_id WHERE po.brand_key = %s AND po.status <> 'cancelled' "
        "AND po.created_at >= %s ORDER BY po.created_at", (b, cutoff))}
    suppliers = {r["supplier_id"]: r["name"] for r in t.all(
        "SELECT supplier_id, name FROM retail_suppliers WHERE brand_key = %s", (b,))}
    delivered = {(r["location_id"], r["variant_id"]): r["at"] for r in t.all(
        "SELECT location_id, variant_id, MAX(created_at) AS at FROM retail_stock_moves WHERE brand_key = %s "
        "AND qty > 0 AND reason IN ('opening', 'grn', 'transfer_in') AND created_at >= %s "
        "GROUP BY location_id, variant_id", (b, cutoff))}
    par: dict[str, dict[str, tuple[int, int]]] = {}
    for r in t.all("SELECT location_id, variant_id, min_qty, max_qty FROM retail_par_levels WHERE brand_key = %s", (b,)):
        par.setdefault(r["location_id"], {})[r["variant_id"]] = (int(r["min_qty"]), int(r["max_qty"]))
    return {"locations": locations, "variants": variants, "on": on, "damaged": damaged, "sold": sold,
            "transfers": transfers, "purchase_orders": purchase_orders, "last_buy": last_buy,
            "suppliers": suppliers, "delivered": delivered, "par": par}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _days_since(iso: Any, now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        at = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - at).total_seconds() / 86400)


def _ref(loc: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not loc:
        return None
    return {"location_id": loc["location_id"], "code": loc["code"], "name": loc["name"], "kind": loc["kind"]}


def _label(var: dict[str, Any]) -> str:
    return f"{var['name']} · {var['color']} / {var['size']}"


def _plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _inr(paise: int) -> str:
    """Whole rupees in Indian grouping: ₹12,34,567."""
    s = str(abs(int(round(paise / 100))))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join([head] + groups + [tail])
    return ("-" if paise < 0 else "") + "₹" + s


def _color_key(raw: Any) -> str:
    return vocab.attr_key("color", raw) or str(raw or "").strip().lower()


def _size_key(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _mix(metrics: Optional[dict[str, Any]], pin: Any) -> Optional[dict[str, Any]]:
    if not metrics or not str(pin or "").strip():
        return None
    mix = insights.demand_mix(metrics, str(pin))
    return mix if mix.get("basis") != "none" else None


def _weights(variants: dict[str, dict[str, Any]], mix: Optional[dict[str, Any]]) -> dict[str, float]:
    """How strongly local wardrobes ask for each SKU: category x colour x size demand share."""
    if not mix:
        return {}
    cat: dict[tuple[str, str], float] = {}
    out: dict[str, float] = {}
    for vid, var in variants.items():
        key = (str(var["category"] or ""), str(var["name"] or ""))
        if key not in cat:
            cat[key] = insights._category_demand(key[0], [key[1]], mix["commodity"])
        if cat[key] <= 0:
            continue
        out[vid] = (cat[key] * (mix["color"].get(_color_key(var["color"]), 0.0) + insights.EPS)
                    * (mix["size"].get(_size_key(var["size"]), 0.0) + insights.EPS))
    return out


def _pct(x: float) -> int:
    return int(round(x * 100))


# ---------------------------------------------------------------------------
# Wardrobe fit
# ---------------------------------------------------------------------------


def _shares(d: dict[str, float]) -> dict[str, float]:
    tot = sum(d.values())
    return {k: x / tot for k, x in d.items() if x > 0} if tot > 0 else {}


def _fit(store: dict[str, Any], units: dict[str, int], variants: dict[str, dict[str, Any]],
         mix: Optional[dict[str, Any]]) -> dict[str, Any]:
    total = sum(n for vid, n in units.items() if vid in variants)
    out: dict[str, Any] = {"location": _ref(store), "pin_code": store.get("pin_code"), "city": store.get("city"),
                           "units": total, "fit": None, "dims": {}, "gaps": [], "excess": [], "demand_top": {},
                           "basis": (mix or {}).get("basis", "none"), "garments": int((mix or {}).get("garments") or 0)}
    if not mix:
        out["note"] = "No SuffAI wardrobe data around this store's PIN yet."
        return out
    labels = mix.get("labels") or {}
    for dim, key in (("category", "commodity"), ("color", "color"), ("size", "size")):
        out["demand_top"][dim] = [{"label": labels.get(f"{key}:{k}", k), "pct": _pct(s)}
                                  for k, s in sorted(mix[key].items(), key=lambda kv: -kv[1])[:3]]
    if total <= 0:
        out["note"] = "No stock at this store yet."
        return out

    names: dict[str, set[str]] = {}
    color_label: dict[str, str] = {}
    sizes: set[str] = set()
    for var in variants.values():
        names.setdefault(str(var["category"] or "Other"), set()).add(str(var["name"] or ""))
        color_label.setdefault(_color_key(var["color"]), str(var["color"]))
        sizes.add(_size_key(var["size"]))
    stock_mix: dict[str, dict[str, float]] = {"category": {}, "color": {}, "size": {}}
    for vid, n in units.items():
        var = variants.get(vid)
        if not var or n <= 0:
            continue
        for dim, k in (("category", str(var["category"] or "Other")), ("color", _color_key(var["color"])),
                       ("size", _size_key(var["size"]))):
            stock_mix[dim][k] = stock_mix[dim].get(k, 0.0) + n
    demand = {
        "category": {c: insights._category_demand(c, sorted(ns), mix["commodity"]) for c, ns in names.items()},
        "color": {k: mix["color"].get(k, 0.0) for k in color_label},
        "size": {k: mix["size"].get(k, 0.0) for k in sizes},
    }
    label_of = {"category": lambda k: k, "color": lambda k: color_label.get(k, k), "size": lambda k: k}
    gaps: list[tuple[float, dict[str, Any]]] = []
    excess: list[tuple[float, dict[str, Any]]] = []
    scores = []
    for dim in ("category", "color", "size"):
        s, d = _shares(stock_mix[dim]), _shares(demand[dim])
        if not s or not d:
            continue
        keys = set(s) | set(d)
        score = 1.0 - 0.5 * sum(abs(s.get(k, 0.0) - d.get(k, 0.0)) for k in keys)
        out["dims"][dim] = _pct(score)
        scores.append(score)
        for k in keys:
            diff = d.get(k, 0.0) - s.get(k, 0.0)
            row = {"dim": dim, "label": label_of[dim](k), "demand_pct": _pct(d.get(k, 0.0)), "stock_pct": _pct(s.get(k, 0.0))}
            if diff >= FIT_GAP:
                gaps.append((diff, row))
            elif -diff >= FIT_EXCESS:
                excess.append((-diff, row))
    if scores:
        out["fit"] = _pct(sum(scores) / len(scores))
    out["gaps"] = [r for _, r in sorted(gaps, key=lambda x: (-round(x[0], 6), x[1]["dim"], x[1]["label"]))[:3]]
    out["excess"] = [r for _, r in sorted(excess, key=lambda x: (-round(x[0], 6), x[1]["dim"], x[1]["label"]))[:2]]
    return out


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _action(kind: str, severity: str, title: str, summary: str, why: list[str], *, key: str,
            frm: Optional[dict[str, Any]] = None, to: Optional[dict[str, Any]] = None,
            at: Optional[dict[str, Any]] = None, lines: Optional[list[dict[str, Any]]] = None,
            impact: int = 0, value_cost: int = 0, fix: dict[str, Any], supplier: Optional[dict[str, Any]] = None,
            doc: Optional[str] = None) -> dict[str, Any]:
    lines = lines or []
    return {
        "id": hashlib.sha1(f"{kind}|{key}".encode()).hexdigest()[:12],
        "kind": kind, "severity": severity, "title": title, "summary": summary, "why": why,
        "from": _ref(frm), "to": _ref(to), "at": _ref(at or to or frm), "supplier": supplier, "document": doc,
        "lines": lines[:MAX_ACTION_LINES], "more_lines": max(0, len(lines) - MAX_ACTION_LINES),
        "units": sum(int(x.get("qty") or 0) for x in lines), "skus": len(lines),
        "impact_paise": int(impact), "value_cost_paise": int(value_cost), "fix": fix,
    }


def _line(var: dict[str, Any], qty: int, **extra: Any) -> dict[str, Any]:
    return {"variant_id": var["variant_id"], "sku": var["sku"], "label": _label(var), "name": var["name"],
            "color": var["color"], "size": var["size"], "category": var["category"], "qty": int(qty),
            "mrp": gst.rupees(int(var["mrp_paise"] or 0)), **extra}


def _can_apply(scope: Scope, fix: dict[str, Any], frm: Optional[dict[str, Any]]) -> bool:
    if fix["type"] == "transfer":
        if scope.role not in PERMISSIONS["transfer.write"] or not frm:
            return False
        if frm["kind"] != "warehouse" and scope.role not in ("brand", "admin"):
            return False
        return not scope.is_staff or frm["location_id"] == scope.location_id
    if fix["type"] == "purchase_order":
        return scope.role in PERMISSIONS["purchase.write"]
    return False


def _plan(scope: Scope, d: dict[str, Any], metrics: Optional[dict[str, Any]], now: datetime) -> dict[str, Any]:
    V = d["variants"]
    locs = {x["location_id"]: x for x in d["locations"]}
    stores = [x for x in d["locations"] if x["kind"] == "store"]
    warehouses = [x for x in d["locations"] if x["kind"] == "warehouse"]

    def have(loc: str, vid: str) -> int:
        return d["on"].get(loc, {}).get(vid, 0)

    incoming: dict[str, dict[str, int]] = {}
    reserved: dict[str, dict[str, int]] = {}
    on_road: dict[str, int] = {}
    for tr in d["transfers"].values():
        for ln in tr["lines"]:
            m = incoming.setdefault(tr["to_location_id"], {})
            m[ln["variant_id"]] = m.get(ln["variant_id"], 0) + ln["qty"]
            if tr["status"] == "draft":
                r = reserved.setdefault(tr["from_location_id"], {})
                r[ln["variant_id"]] = r.get(ln["variant_id"], 0) + ln["qty"]
            else:
                on_road[ln["variant_id"]] = on_road.get(ln["variant_id"], 0) + ln["qty"]
    on_order: dict[str, int] = {}
    for po in d["purchase_orders"].values():
        for ln in po["lines"]:
            on_order[ln["variant_id"]] = on_order.get(ln["variant_id"], 0) + ln["open"]

    def inc(loc: str, vid: str) -> int:
        return incoming.get(loc, {}).get(vid, 0)

    def daily(loc: str, vid: str) -> float:
        return d["sold"].get(loc, {}).get(vid, 0) / LOOKBACK_DAYS

    mixes = {s["location_id"]: _mix(metrics, s.get("pin_code")) for s in stores}
    weights = {sid: _weights(V, mix) for sid, mix in mixes.items()}
    top_w = {sid: max(w.values(), default=0.0) for sid, w in weights.items()}

    avail = {w["location_id"]: {vid: max(0, n - reserved.get(w["location_id"], {}).get(vid, 0))
                                for vid, n in d["on"].get(w["location_id"], {}).items()} for w in warehouses}
    wh_start = {w: dict(m) for w, m in avail.items()}

    def spare_start(sid: str, vid: str) -> int:
        n = have(sid, vid) - reserved.get(sid, {}).get(vid, 0)
        rate = daily(sid, vid)
        if rate > 0:
            keep = math.ceil(rate * KEEP_COVER_DAYS)
        else:
            age = _days_since(d["delivered"].get((sid, vid)), now)
            keep = n if age is not None and age < SLOW_AGE_DAYS else 1
        if vid in d["par"].get(sid, {}):
            keep = max(keep, d["par"][sid][vid][1])
        return max(0, n - keep)

    spare: dict[tuple[str, str], int] = {}

    def spare_of(sid: str, vid: str) -> int:
        if (sid, vid) not in spare:
            spare[(sid, vid)] = spare_start(sid, vid)
        return spare[(sid, vid)]

    # 1. What each store needs: par levels, low cover at its sales pace, and unstocked local favourites.
    needs: list[dict[str, Any]] = []
    for s in stores:
        sid = s["location_id"]
        par = d["par"].get(sid, {})
        for vid in set(d["sold"].get(sid, {})) | set(par):
            if vid not in V:
                continue
            h, i, rate = have(sid, vid), inc(sid, vid), daily(sid, vid)
            if vid in par:
                lo, hi = par[vid]
                if h + i >= lo:
                    continue
                need, basis = hi - h - i, "par"
            else:
                if (h + i) / rate >= LOW_COVER_DAYS:
                    continue
                need, basis = math.ceil(rate * TARGET_COVER_DAYS) - h - i, "sales"
            if need > 0:
                needs.append({"store": sid, "vid": vid, "need": need, "basis": basis, "daily": rate, "have": h,
                              "inc": i, "cover": (h + i) / rate if rate else 0.0})
        w, top = weights.get(sid) or {}, top_w.get(sid) or 0.0
        if top > 0:
            gaps = [vid for vid, x in w.items()
                    if x >= GAP_TOP_SHARE * top and have(sid, vid) + inc(sid, vid) == 0
                    and vid not in d["sold"].get(sid, {}) and vid not in par
                    and any(m.get(vid, 0) > 0 for m in avail.values())]
            for vid in sorted(gaps, key=lambda k: (-w[k], V[k]["sku"]))[:GAP_MAX_PER_STORE]:
                needs.append({"store": sid, "vid": vid, "need": SEED_UNITS, "basis": "demand", "daily": 0.0,
                              "have": 0, "inc": 0, "cover": None, "match": w[vid] / top})
    needs.sort(key=lambda n: (n["basis"] == "demand", n["cover"] if n["cover"] is not None else 0.0,
                              -n.get("match", 0.0), -n["need"], V[n["vid"]]["sku"]))

    # 2. Fill them: warehouses first, then stores with spare units.
    moves: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    short: dict[str, int] = {}
    planned_in: dict[tuple[str, str], int] = {}
    lent: dict[tuple[str, str], int] = {}

    def add(kind: str, frm: str, n: dict[str, Any], qty: int, source_units: int) -> None:
        vid = n["vid"]
        planned_in[(n["store"], vid)] = planned_in.get((n["store"], vid), 0) + qty
        if kind == "rebalance":
            lent[(frm, vid)] = lent.get((frm, vid), 0) + qty
        group = moves.setdefault((kind, frm, n["store"]), {})
        if vid in group:
            group[vid]["qty"] += qty
            return
        extra = {"basis": n["basis"], "daily_sales": round(n["daily"], 2), "on_hand": n["have"], "incoming": n["inc"],
                 "cover_days": None if n["cover"] is None or not n["daily"] else round(n["cover"], 1),
                 "need": n["need"], "source_units": source_units}
        if "match" in n:
            extra["match_pct"] = _pct(n["match"])
        group[vid] = _line(V[vid], qty, **extra)

    for n in needs:
        vid, left = n["vid"], n["need"]
        for wid in sorted(avail, key=lambda w: (-avail[w].get(vid, 0), locs[w]["code"])):
            give = min(left, avail[wid].get(vid, 0))
            if give <= 0:
                break
            add("restock", wid, n, give, wh_start[wid].get(vid, 0))
            avail[wid][vid] -= give
            left -= give
            if not left:
                break
        if left and n["basis"] != "demand":
            donors = sorted((s["location_id"] for s in stores if s["location_id"] != n["store"]),
                            key=lambda sid: (-spare_of(sid, vid), locs[sid]["code"]))
            for sid in donors:
                give = min(left, spare_of(sid, vid))
                if give <= 0:
                    break
                add("rebalance", sid, n, give, have(sid, vid) - reserved.get(sid, {}).get(vid, 0))
                spare[(sid, vid)] -= give
                left -= give
                if not left:
                    break
        if left:
            short[vid] = short.get(vid, 0) + left

    # 3. Slow stock: send it where it sells or where wardrobes ask for it; report what is left.
    slow: dict[str, list[dict[str, Any]]] = {}
    for s in stores:
        sid = s["location_id"]
        for vid, h in sorted(d["on"].get(sid, {}).items()):
            if vid not in V or h < SLOW_MIN_UNITS or daily(sid, vid) > 0:
                continue
            age = _days_since(d["delivered"].get((sid, vid)), now)
            if age is not None and age < SLOW_AGE_DAYS:
                continue
            best, best_score = None, 0.0
            for other in stores:
                oid = other["location_id"]
                if oid == sid:
                    continue
                rate = daily(oid, vid)
                if rate > 0:
                    score = 1.0 + rate
                elif top_w.get(oid) and have(oid, vid) + inc(oid, vid) == 0:
                    score = weights[oid].get(vid, 0.0) / top_w[oid]
                    if score < GAP_TOP_SHARE:
                        continue
                else:
                    continue
                if score > best_score:
                    best, best_score = oid, score
            if best is not None:
                rate = daily(best, vid)
                target = math.ceil(rate * TARGET_COVER_DAYS) if rate > 0 else SEED_UNITS
                want = target - have(best, vid) - inc(best, vid) - planned_in.get((best, vid), 0)
                give = min(want, spare_of(sid, vid))
                if give > 0:
                    n = {"store": best, "vid": vid, "need": want, "basis": "slow", "daily": rate,
                         "have": have(best, vid), "inc": inc(best, vid),
                         "cover": (have(best, vid) + inc(best, vid)) / rate if rate else None}
                    if rate <= 0:
                        n["match"] = best_score
                    add("rebalance", sid, n, give, h - reserved.get(sid, {}).get(vid, 0))
                    spare[(sid, vid)] -= give
            going = lent.get((sid, vid), 0)
            if h - going >= SLOW_MIN_UNITS:
                var = V[vid]
                slow.setdefault(sid, []).append(_line(
                    var, h - going, basis="slow", on_hand=h, moving=going, age_days=None if age is None else int(age),
                    value_cost=gst.rupees(int(var["avg_cost_paise"] or 0) * (h - going))))

    actions: list[dict[str, Any]] = []
    for (kind, frm_id, to_id), group in moves.items():
        frm, to = locs[frm_id], locs[to_id]
        lines = list(group.values())
        lines.sort(key=lambda x: ({"sales": 0, "par": 0, "slow": 1, "demand": 2}[x["basis"]],
                                  x["cover_days"] if x["cover_days"] is not None else 0.0, x["sku"]))
        units = sum(x["qty"] for x in lines)
        impact = sum(int(V[x["variant_id"]]["mrp_paise"] or 0) * x["qty"] for x in lines)
        fix = {"type": "transfer", "perm": "transfer.write"}
        out_ = [x for x in lines if x["basis"] in ("sales", "par") and x["on_hand"] == 0 and x["incoming"] == 0]
        low = [x for x in lines if x["basis"] == "sales" and x not in out_]
        par_ = [x for x in lines if x["basis"] == "par" and x not in out_]
        gap = [x for x in lines if x["basis"] == "demand"]
        slow_ = [x for x in lines if x["basis"] == "slow"]
        why: list[str] = []
        if out_:
            why.append(f"{_plural(len(out_), 'SKU')} {'is' if len(out_) == 1 else 'are'} out of stock at {to['name']} "
                       "and still selling")
        if low:
            why.append(f"{_plural(len(low), 'SKU')} {'has' if len(low) == 1 else 'have'} under {LOW_COVER_DAYS} days "
                       f"of stock left at the last {LOOKBACK_DAYS} days' pace")
        if par_:
            why.append(f"{_plural(len(par_), 'SKU')} {'is' if len(par_) == 1 else 'are'} below the par level you set")
        fast = max((x for x in lines if x["basis"] in ("sales", "par") and x["daily_sales"]),
                   key=lambda x: x["daily_sales"], default=None)
        if fast:
            left_txt = "none left" if fast["on_hand"] + fast["incoming"] == 0 else \
                f"{fast['cover_days']:g} days left"
            why.append(f"Fastest seller: {fast['label']}, {fast['daily_sales'] * 7:.1f} a week, {left_txt}")
        if gap:
            mix = mixes.get(to_id) or {}
            why.append(f"{_plural(len(gap), 'SKU')} {'is' if len(gap) == 1 else 'are'} not stocked yet but "
                       f"{'matches' if len(gap) == 1 else 'match'} what shoppers around PIN {to.get('pin_code')} wear"
                       + (f" ({_plural(int(mix.get('garments') or 0), 'garment')} seen)" if mix.get("garments") else ""))
        if slow_:
            why.append(f"{_plural(sum(x['qty'] for x in slow_), 'unit')} haven't sold at {frm['name']} in "
                       f"{LOOKBACK_DAYS} days; they sell or fit better at {to['name']}")
        if kind == "restock":
            severity = "high" if out_ or any((x["cover_days"] or 0) < 3 for x in low) else ("low" if not (low or par_) else "medium")
            title = f"Restock {to['name']}"
            summary = f"Send {_plural(units, 'unit')} of {_plural(len(lines), 'SKU')} from {frm['code']} to {to['code']}."
            why.append(f"{frm['name']} has the units to send now")
        else:
            severity = "medium" if (out_ or low or par_) else "low"
            title = f"Move stock {frm['code']} → {to['code']}"
            summary = (f"Move {_plural(units, 'unit')} of {_plural(len(lines), 'SKU')} from {frm['name']} "
                       f"to {to['name']}.")
            if out_ or low or par_:
                why.append(f"No warehouse has enough; {frm['name']} keeps {KEEP_COVER_DAYS}+ days of its own cover")
        actions.append(_action(
            kind, severity, title, summary, why, key=f"{frm_id}|{to_id}", frm=frm, to=to, lines=lines, impact=impact,
            value_cost=sum(int(V[x["variant_id"]]["avg_cost_paise"] or 0) * x["qty"] for x in lines), fix=fix))

    # 4. Reorders: brand-wide cover, grouped by last supplier and receiving warehouse.
    wh_ids = {w["location_id"] for w in warehouses}
    orders: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for vid, var in V.items():
        rate = sum(daily(s["location_id"], vid) for s in stores)
        if rate <= 0 or not warehouses:
            continue
        units = sum(have(x, vid) for x in locs) + on_road.get(vid, 0) + on_order.get(vid, 0)
        cover = units / rate
        if cover >= REORDER_COVER_DAYS:
            continue
        qty = math.ceil(rate * ORDER_COVER_DAYS) - units
        if qty <= 0:
            continue
        last = d["last_buy"].get(vid)
        sup = str(last["supplier_id"]) if last and last["supplier_id"] in d["suppliers"] else ""
        dest = last["location_id"] if last and last["location_id"] in wh_ids else \
            max(warehouses, key=lambda w: (have(w["location_id"], vid), w["code"]))["location_id"]
        cost = int(last["unit_cost_paise"]) if last and last.get("unit_cost_paise") else int(var["avg_cost_paise"] or 0)
        orders.setdefault((sup, dest), []).append(_line(
            var, qty, basis="network", daily_sales=round(rate, 2), cover_days=round(cover, 1), network_units=units,
            on_order=on_order.get(vid, 0), short=short.get(vid, 0), unit_cost=gst.rupees(cost), unit_cost_paise=cost))
    for (sup, dest), lines in orders.items():
        lines.sort(key=lambda x: (x["cover_days"], x["sku"]))
        to = locs[dest]
        name = d["suppliers"].get(sup)
        units = sum(x["qty"] for x in lines)
        worst = lines[0]
        why = [f"{_plural(len(lines), 'SKU')} will run out brand-wide within {REORDER_COVER_DAYS} days at the last "
               f"{LOOKBACK_DAYS} days' pace, counting every store, warehouse, transfer on the road and open PO",
               f"Tightest: {worst['label']}, {worst['cover_days']:g} days of cover across the brand",
               f"Orders {ORDER_COVER_DAYS} days of cover into {to['name']}"]
        if any(x["short"] for x in lines):
            why.append("Stores are already short of some of these and no location can cover them")
        why.append(f"Last bought from {name}" if name else "No supplier on record for these: pick one when you create it")
        cost = sum(x["unit_cost_paise"] * x["qty"] for x in lines)
        actions.append(_action(
            "reorder", "high" if worst["cover_days"] < 7 else "medium",
            f"Reorder from {name}" if name else "Reorder (pick a supplier)",
            f"Draft a PO for {_plural(units, 'unit')} of {_plural(len(lines), 'SKU')} into {to['code']}, about {_inr(cost)} at cost.",
            why, key=f"{sup}|{dest}", to=to, lines=lines,
            impact=sum(int(V[x["variant_id"]]["mrp_paise"] or 0) * x["qty"] for x in lines), value_cost=cost,
            fix={"type": "purchase_order", "perm": "purchase.write"},
            supplier={"supplier_id": sup, "name": name} if name else None))

    # 5. Slow stock left after rebalancing.
    for sid, lines in slow.items():
        loc = locs[sid]
        units = sum(x["qty"] for x in lines)
        cost = sum(int(V[x["variant_id"]]["avg_cost_paise"] or 0) * x["qty"] for x in lines)
        lines.sort(key=lambda x: (-x["qty"], x["sku"]))
        going = sum(x["moving"] for x in lines)
        actions.append(_action(
            "slow", "low", f"Slow stock at {loc['name']}",
            f"{_plural(units, 'unit')} of {_plural(len(lines), 'SKU')} haven't sold in {LOOKBACK_DAYS} days, {_inr(cost)} at cost.",
            [f"No net sales at {loc['name']} for {LOOKBACK_DAYS} days, delivered {SLOW_AGE_DAYS}+ days ago",
             f"{_plural(going, 'more unit')} of these {'is' if going == 1 else 'are'} already suggested for stores "
             "that sell them" if going else "No other store sells them or asks for them yet",
             "Try a markdown at the till or a campaign around this store's PIN"],
            key=sid, at=loc, lines=lines, value_cost=cost, fix={"type": "open", "tab": "inventory", "section": "stock"}))

    # 6. Documents stuck in the flow, and damaged stock.
    today = db.today_ist()
    for tr in d["transfers"].values():
        frm, to = locs.get(tr["from_location_id"]), locs.get(tr["to_location_id"])
        if not frm or not to:
            continue
        units = sum(x["qty"] for x in tr["lines"])
        value = sum(int((V.get(x["variant_id"]) or {}).get("mrp_paise") or 0) * x["qty"] for x in tr["lines"])
        if tr["status"] == "in_transit":
            age = _days_since(tr["dispatched_at"], now)
            if age is None or age < STALE_TRANSIT_DAYS:
                continue
            actions.append(_action(
                "receive", "medium", f"Receive {tr['transfer_number']} at {to['name']}",
                f"{_plural(units, 'unit')} from {frm['code']} have been on the road {int(age)} days.",
                [f"Dispatched {int(age)} days ago from {frm['name']}",
                 "Units on the road can't be sold until the store receives them"],
                key=tr["transfer_id"], frm=frm, to=to, at=to, impact=value, doc=tr["transfer_number"],
                fix={"type": "open", "tab": "inventory", "section": "incoming"}))
        else:
            age = _days_since(tr["created_at"], now)
            if age is None or age < STALE_DRAFT_DAYS:
                continue
            actions.append(_action(
                "dispatch", "medium", f"Dispatch {tr['transfer_number']} to {to['name']}",
                f"A draft for {_plural(units, 'unit')} has waited {int(age)} days at {frm['code']}.",
                [f"Drafted {int(age)} days ago" + (" from a Stock Pilot suggestion" if tr["source"] == "aura" else ""),
                 f"The units stay held at {frm['name']} until it is dispatched or cancelled"],
                key=tr["transfer_id"], frm=frm, to=to, at=frm, impact=value, doc=tr["transfer_number"],
                fix={"type": "open", "tab": "warehouse", "section": "transfers"}))
    for po in d["purchase_orders"].values():
        exp = str(po["expected_date"] or "")[:10]
        if po["status"] not in ("ordered", "partial") or not exp or exp >= today:
            continue
        to = locs.get(po["location_id"])
        if not to:
            continue
        open_units = sum(x["open"] for x in po["lines"])
        late = (date.fromisoformat(today) - date.fromisoformat(exp)).days
        sup = d["suppliers"].get(po["supplier_id"]) or "the supplier"
        actions.append(_action(
            "po_late", "medium", f"{po['po_number']} from {sup} is late",
            f"{_plural(open_units, 'unit')} were due at {to['code']} {_plural(late, 'day')} ago.",
            [f"Expected {exp}; {_plural(open_units, 'unit')} still open",
             "Chase the supplier, or cancel and reorder if it won't come"],
            key=po["po_id"], to=to, at=to, doc=po["po_number"],
            fix={"type": "open", "tab": "warehouse", "section": "po"}))
    for lid, m in d["damaged"].items():
        loc = locs.get(lid)
        units = sum(m.values())
        if not loc or units <= 0:
            continue
        cost = sum(int((V.get(vid) or {}).get("avg_cost_paise") or 0) * n for vid, n in m.items())
        actions.append(_action(
            "damaged", "low", f"{_plural(units, 'damaged unit')} at {loc['name']}",
            f"{_inr(cost)} at cost is sitting in the damaged bin.",
            [f"{_plural(len(m), 'SKU')} in the damaged bin",
             "Write them off or send them back to the supplier from Adjust & count"],
            key=lid, at=loc, value_cost=cost, fix={"type": "open", "tab": "inventory", "section": "tools"}))

    for a in actions:
        a["can_apply"] = _can_apply(scope, a["fix"], a["from"])
    if scope.is_staff:
        me = scope.location_id
        actions = [a for a in actions if me in {(a.get(k) or {}).get("location_id") for k in ("from", "to", "at")}]
    actions.sort(key=lambda a: (_SEVERITY[a["severity"]], -a["impact_paise"], -a["value_cost_paise"], a["title"]))

    fits = [_fit(s, d["on"].get(s["location_id"], {}), V, mixes.get(s["location_id"])) for s in stores
            if not (scope.role == "store_staff" and s["location_id"] != scope.location_id)]
    fits.sort(key=lambda f: (f["fit"] is None, f["fit"] if f["fit"] is not None else 0, f["location"]["code"]))
    return {"actions": actions, "stores": fits, "needs": needs, "has_demand": any(mixes.values())}


def _brief(actions: list[dict[str, Any]], fits: list[dict[str, Any]], has_demand: bool) -> dict[str, Any]:
    move = sum(a["units"] for a in actions if a["kind"] in ("restock", "rebalance"))
    order = sum(a["units"] for a in actions if a["kind"] == "reorder")
    stuck = sum(1 for a in actions if a["kind"] in ("receive", "dispatch", "po_late"))
    slow = sum(a["units"] for a in actions if a["kind"] == "slow")
    urgent = sum(1 for a in actions if a["severity"] == "high")
    bits = []
    if move:
        bits.append(f"move {_plural(move, 'unit')} to stores")
    if order:
        bits.append(f"reorder {order:,} from suppliers")
    if stuck:
        bits.append(f"clear {_plural(stuck, 'stuck document')}")
    if slow:
        bits.append(f"rework {_plural(slow, 'slow unit')}")
    if bits:
        text = _join(bits)
        headline = text[0].upper() + text[1:] + "."
    else:
        headline = f"All clear: every store has at least {LOW_COVER_DAYS} days of stock at its current pace."
    scored = [f["fit"] for f in fits if f["fit"] is not None]
    weakest = next((f for f in fits if f["fit"] is not None), None)
    points = [a["summary"] for a in actions[:3]]
    if weakest and weakest["gaps"]:
        g = weakest["gaps"][0]
        points.append(f"{weakest['location']['name']} is the weakest wardrobe fit ({weakest['fit']}/100): "
                      f"{g['label']} is {g['demand_pct']}% of local demand but {g['stock_pct']}% of its stock.")
    return {"headline": headline, "urgent": urgent, "points": points[:4],
            "avg_fit": round(sum(scored) / len(scored)) if scored else None,
            "demand": "SuffAI wardrobe data around your stores" if has_demand else None}


def build(scope: Scope, metrics: Optional[dict[str, Any]]) -> dict[str, Any]:
    scope.require("read")
    with db.tx() as t:
        data = _load(t, scope.brand_key)
    plan = _plan(scope, data, metrics, db.now())
    actions, fits = plan["actions"], plan["stores"]
    counts: dict[str, int] = {}
    for a in actions:
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
    store_ids = {s["location_id"] for s in data["locations"] if s["kind"] == "store"}
    risk = [n for n in plan["needs"] if n["basis"] in ("sales", "par")
            and (scope.role != "store_staff" or n["store"] == scope.location_id)]
    return {
        "label": insights.ACTUAL_LABEL,
        "generated_at": db.now_iso(),
        "lookback_days": LOOKBACK_DAYS,
        "brief": _brief(actions, fits, plan["has_demand"]),
        "kpis": {
            "actions": len(actions),
            "urgent": sum(1 for a in actions if a["severity"] == "high"),
            "stockouts": sum(1 for n in risk if n["have"] + n["inc"] == 0),
            "at_risk": len(risk),
            "units_to_move": sum(a["units"] for a in actions if a["kind"] in ("restock", "rebalance")),
            "units_to_order": sum(a["units"] for a in actions if a["kind"] == "reorder"),
            "sales_value_paise": sum(a["impact_paise"] for a in actions if a["kind"] in MOVE_KINDS),
            "slow_value_paise": sum(a["value_cost_paise"] for a in actions if a["kind"] == "slow"),
            "stores": len(store_ids),
        },
        "counts": counts,
        "actions": actions,
        "stores": fits,
        "method": list(METHOD),
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _reviewed(act: dict[str, Any], edits: Any) -> list[dict[str, Any]]:
    by_id = {x["variant_id"]: x for x in act["lines"]}
    if edits is None:
        picked = [{"variant_id": x["variant_id"], "qty": x["qty"]} for x in act["lines"]]
    else:
        if not isinstance(edits, list):
            raise RetailError("Quantities must be a list of lines.")
        if len(edits) > v.MAX_LINES:
            raise RetailError(f"At most {v.MAX_LINES} items per document.")
        picked, seen = [], set()
        for e in edits:
            if not isinstance(e, dict):
                raise RetailError("Each line must be an object.")
            vid = str(e.get("variant_id") or "")
            if vid not in by_id or vid in seen:
                raise RetailError("A line isn't part of this suggestion. Refresh and try again.")
            seen.add(vid)
            q = v.qty(e.get("qty"), lo=0)
            if q:
                picked.append({"variant_id": vid, "qty": q})
    if not picked:
        raise RetailError("All quantities are 0.")
    if act["fix"]["type"] == "transfer":
        for p in picked:
            ln = by_id[p["variant_id"]]
            if p["qty"] > int(ln.get("source_units") or 0):
                raise RetailError(f"Only {ln.get('source_units') or 0} of {ln['sku']} can be sent from "
                                  f"{act['from']['code']}.", 409)
    return picked


def find(scope: Scope, metrics: Optional[dict[str, Any]], action_id: str) -> dict[str, Any]:
    plan = build(scope, metrics)
    act = next((a for a in plan["actions"] if a["id"] == action_id), None)
    if act is None:
        raise RetailError("This suggestion has changed since you opened it. Refresh the Stock Pilot.", 409)
    return act


def apply(scope: Scope, metrics: Optional[dict[str, Any]], action_id: str, data: Optional[dict[str, Any]] = None
          ) -> dict[str, Any]:
    """Turn a suggestion into a draft transfer or draft PO. Recomputed first, so a stale card can't apply."""
    data = data or {}
    act = find(scope, metrics, action_id)
    fix = act["fix"]
    if fix["type"] not in ("transfer", "purchase_order"):
        raise RetailError("This one needs a person: open it to act.")
    if not act["can_apply"]:
        raise RetailError("Your login can't create this. Ask a brand admin.", 403)
    lines = _reviewed(act, data.get("lines"))
    note = f"AURA Stock Pilot: {act['title']}"[:300]
    if fix["type"] == "transfer":
        tr = wms.create_transfer(scope, {"from_location_id": act["from"]["location_id"],
                                         "to_location_id": act["to"]["location_id"], "lines": lines,
                                         "note": note, "source": "aura"})
        return {"type": "transfer", "transfer_id": tr["transfer_id"], "number": tr["transfer_number"],
                "units": sum(x["qty"] for x in lines), "skus": len(lines), "from": act["from"], "to": act["to"],
                "title": act["title"]}
    costs = {x["variant_id"]: x.get("unit_cost_paise") for x in act["lines"]}
    supplier_id = str(data.get("supplier_id") or (act.get("supplier") or {}).get("supplier_id") or "")
    po = wms.create_po(scope, {
        "supplier_id": supplier_id, "location_id": act["to"]["location_id"], "note": note,
        "lines": [{"variant_id": x["variant_id"], "qty": x["qty"],
                   **({"unit_cost": f"{costs[x['variant_id']] / 100:.2f}"} if costs.get(x["variant_id"]) else {})}
                  for x in lines]})
    return {"type": "purchase_order", "po_id": po["po_id"], "number": po["po_number"],
            "units": sum(x["qty"] for x in lines), "skus": len(lines), "to": act["to"], "title": act["title"],
            "supplier": po.get("supplier_name")}


# ---------------------------------------------------------------------------
# Stock lookup ("where are the navy shirts in M?")
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\s,;/]+")
_STOP = {"the", "a", "an", "of", "in", "size", "colour", "color", "stock", "units", "how", "many", "much", "do",
         "we", "have", "where", "is", "are", "our", "my", "any", "left", "for", "and", "at"}


def _matches(var: dict[str, Any], word: str) -> bool:
    if word == str(var["size"] or "").lower() or word == str(var["sku"] or "").lower():
        return True
    if vocab.attr_key("color", word) and vocab.attr_key("color", word) == _color_key(var["color"]):
        return True
    if len(word) < 3:
        return False
    stem = word[:-1] if word.endswith("s") and len(word) > 3 else word
    hay = " ".join(str(var[k] or "").lower() for k in ("sku", "style_code", "name", "category", "color"))
    return stem in hay


def stock_lookup(scope: Scope, query: str, *, limit: int = 8) -> dict[str, Any]:
    """Every SKU matching all words of `query` (style, name, category, colour, size or SKU), with units per
    location, units on the road, open PO units, 28-day sales and days of cover."""
    scope.require("read")
    words = [w for w in (m.group(0).lower().strip(".?!") for m in _WORD_RE.finditer(query or "")) if w and w not in _STOP]
    if not words:
        return {"query": query, "matches": 0, "skus": []}
    with db.tx() as t:
        d = _load(t, scope.brand_key)
    hits = sorted((var for var in d["variants"].values() if all(_matches(var, w) for w in words)),
                  key=lambda x: (x["style_code"], x["color"], x["size"]))
    locs = [x for x in d["locations"] if not scope.is_staff or x["location_id"] == scope.location_id]
    road: dict[tuple[str, str], int] = {}
    for tr in d["transfers"].values():
        if tr["status"] == "in_transit":
            for ln in tr["lines"]:
                road[(tr["to_location_id"], ln["variant_id"])] = road.get((tr["to_location_id"], ln["variant_id"]), 0) + ln["qty"]
    out = []
    for var in hits[:limit]:
        vid = var["variant_id"]
        rows, units, sold = [], 0, 0
        for loc in locs:
            n = d["on"].get(loc["location_id"], {}).get(vid, 0)
            s = d["sold"].get(loc["location_id"], {}).get(vid, 0)
            r = road.get((loc["location_id"], vid), 0)
            units += n
            sold += s
            if n or s or r:
                rows.append({"code": loc["code"], "name": loc["name"], "kind": loc["kind"], "units": n, "on_the_way": r,
                             "sold_28d": s, "cover_days": round(n / (s / LOOKBACK_DAYS), 1) if s else None})
        open_po = sum(ln["open"] for po in d["purchase_orders"].values() for ln in po["lines"] if ln["variant_id"] == vid)
        out.append({"sku": var["sku"], "label": _label(var), "category": var["category"], "mrp": gst.rupees(var["mrp_paise"]),
                    "units": units, "sold_28d": sold, "open_po_units": open_po,
                    "cover_days": round(units / (sold / LOOKBACK_DAYS), 1) if sold else None, "locations": rows})
    return {"query": query, "matches": len(hits), "skus": out, "more": max(0, len(hits) - limit),
            "label": insights.ACTUAL_LABEL, "lookback_days": LOOKBACK_DAYS}


def resolve_transfer(scope: Scope, from_ref: str, to_ref: str, items: list[tuple[str, int]]) -> dict[str, Any]:
    """A transfer the brand asked for in words: locations by code or name, SKUs by exact code, checked
    against what the source can send now (sellable, less units held by its drafts)."""
    scope.require("transfer.write")
    with db.tx() as t:
        locs = t.all("SELECT location_id, code, name, kind FROM retail_locations WHERE brand_key = %s AND active = TRUE",
                     (scope.brand_key,))
        by: dict[str, dict[str, Any]] = {}
        for loc in locs:
            by[loc["code"].lower()] = loc
            by.setdefault(loc["name"].lower(), loc)
        frm, to = by.get(from_ref.strip().lower()), by.get(to_ref.strip().lower())
        if not frm or not to:
            raise RetailError("Name both locations by their code, e.g. WH1 or BLR01.")
        if frm["location_id"] == to["location_id"]:
            raise RetailError("Pick two different locations.")
        scope.check_location(frm["location_id"])
        if frm["kind"] != "warehouse" and scope.role not in ("brand", "admin"):
            raise RetailError("Only brand admins can send stock out of a store.", 403)
        skus = sorted({s.strip().upper() for s, _ in items})
        marks = ",".join(["%s"] * len(skus))
        rows = {r["sku"].upper(): r for r in t.all(
            f"SELECT vv.variant_id, vv.sku FROM retail_variants vv JOIN retail_products p ON p.product_id = vv.product_id "
            f"WHERE vv.brand_key = %s AND vv.active = TRUE AND p.active = TRUE AND UPPER(vv.sku) IN ({marks})",
            [scope.brand_key] + skus)}
        missing = [s for s in skus if s not in rows]
        if missing:
            raise RetailError(f"Not in your catalog: {', '.join(missing[:5])}. Use the exact SKU codes.")
        merged: dict[str, int] = {}
        for s, q in items:
            vid = rows[s.strip().upper()]["variant_id"]
            merged[vid] = merged.get(vid, 0) + int(q)
        held = {r["variant_id"]: int(r["n"] or 0) for r in t.all(
            "SELECT tl.variant_id, SUM(tl.qty_requested) AS n FROM retail_transfer_lines tl JOIN retail_transfers tr "
            "ON tr.transfer_id = tl.transfer_id WHERE tr.from_location_id = %s AND tr.status = 'draft' "
            "GROUP BY tl.variant_id", (frm["location_id"],))}
        free = {vid: n - held.get(vid, 0) for vid, n in stock.sellable(t, frm["location_id"], list(merged)).items()}
    sku_of = {r["variant_id"]: r["sku"] for r in rows.values()}
    for vid, q in merged.items():
        if q > free.get(vid, 0):
            raise RetailError(f"{frm['code']} can send only {max(0, free.get(vid, 0))} of {sku_of[vid]} now.")
    return {"from": frm, "to": to, "lines": [{"variant_id": vid, "qty": q} for vid, q in merged.items()]}


# ---------------------------------------------------------------------------
# AURA view (no IDs)
# ---------------------------------------------------------------------------


def _public_loc(ref: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    return {"code": ref["code"], "name": ref["name"], "kind": ref["kind"]} if ref else None


def agent_view(plan: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    acts = []
    for i, a in enumerate(plan["actions"][:limit], start=1):
        acts.append({
            "number": i, "kind": a["kind"], "severity": a["severity"], "title": a["title"], "summary": a["summary"],
            "why": a["why"], "from": _public_loc(a["from"]), "to": _public_loc(a["to"]), "at": _public_loc(a["at"]),
            "units": a["units"], "skus": a["skus"], "document": a["document"],
            "supplier": (a.get("supplier") or {}).get("name"),
            "sales_value_inr_at_mrp": gst.rupees(a["impact_paise"]), "value_at_cost_inr": gst.rupees(a["value_cost_paise"]),
            "can_create_draft": a["can_apply"],
            "lines": [{k: x.get(k) for k in ("sku", "label", "qty", "basis", "daily_sales", "cover_days", "on_hand",
                                              "incoming", "match_pct") if x.get(k) is not None} for x in a["lines"][:10]],
        })
    k = plan["kpis"]
    return {
        "label": plan["label"], "lookback_days": plan["lookback_days"], "brief": plan["brief"],
        "kpis": {**{x: k[x] for x in ("actions", "urgent", "stockouts", "at_risk", "units_to_move", "units_to_order",
                                       "stores")},
                 "sales_value_inr_at_mrp": gst.rupees(k["sales_value_paise"]),
                 "slow_value_inr_at_cost": gst.rupees(k["slow_value_paise"])},
        "actions": acts, "more_actions": max(0, len(plan["actions"]) - limit),
        "stores": [{"store": _public_loc(f["location"]), "pin_code": f["pin_code"], "fit": f["fit"], "dims": f["dims"],
                    "gaps": f["gaps"], "excess": f["excess"], "demand_top": f["demand_top"], "units": f["units"],
                    "note": f.get("note")} for f in plan["stores"]],
        "method": plan["method"],
    }


def describe(a: dict[str, Any]) -> str:
    """Plain-language explanation of one action, for AURA's no-model replies."""
    why = "; ".join(w.rstrip(".") for w in a["why"][:4])
    text = f"{a['title']}. {a['summary']} Why: {why}."
    top = [x for x in a["lines"][:3]]
    if top and a["kind"] in MOVE_KINDS:
        text += " Top lines: " + "; ".join(f"{x['label']} ×{x['qty']}" for x in top) + "."
    return text
