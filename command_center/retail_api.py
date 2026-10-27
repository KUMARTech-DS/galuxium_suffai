"""REST routes for the retail suite (/v1/retail/*): catalog, stock, warehouse, POS, reports, staff.

Every route resolves the caller's Scope from the session: brand logins act on their own brand, admins on
the brand they pass as ?brand=, staff on their brand and only at the location on their login.
RetailError becomes its status with the message; storage failures become a masked 503 with a reference.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

import auth_acl
import brand_campaign as brand_camp
import campaign_service as camp_svc
import retail_catalog as catalog
import retail_db as db
import retail_gst as gst
import retail_insights as insights
import retail_pilot as pilot
import retail_pos as pos
import retail_staff as staff
import retail_stock as stock
import retail_wms as wms
import vocab
from retail_db import RetailError, RetailUnavailable
from retail_scope import PERMISSIONS, RETAIL_ROLES, STAFF_ROLES, Scope

log = logging.getLogger("command_center.retail_api")

MAX_CSV_CHARS = 5_000_000

router = APIRouter(prefix="/v1/retail", tags=["retail"])

_metrics_loader: Optional[Callable[[], dict[str, Any]]] = None
_token_resolver: Optional[Callable[[Request, Optional[str], Optional[str]], Optional[str]]] = None


def init(*, metrics_loader: Callable[[], dict[str, Any]],
         token_resolver: Callable[[Request, Optional[str], Optional[str]], Optional[str]]) -> None:
    global _metrics_loader, _token_resolver
    _metrics_loader, _token_resolver = metrics_loader, token_resolver
    pos.campaign_lookup = _campaign_for
    pos.campaign_list = lambda scope: brand_camp.list_campaigns(brand_name=scope.brand_name, limit=100)


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(RetailError, _retail_error)
    app.add_exception_handler(RetailUnavailable, _retail_unavailable)


async def _retail_error(request: Request, exc: RetailError) -> JSONResponse:
    detail: Any = str(exc)
    if exc.errors:
        detail = {"message": str(exc), "errors": exc.errors}
    return JSONResponse({"detail": detail}, status_code=exc.status)


async def _retail_unavailable(request: Request, exc: RetailUnavailable) -> JSONResponse:
    ref = uuid.uuid4().hex[:12]
    log.error("retail storage unavailable path=%s cause=%s reference=%s", request.url.path, exc, ref,
              exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse({"detail": f"This service is temporarily unavailable. Reference: {ref}"}, status_code=503)


def _campaign_for(scope: Scope, campaign_id: str) -> Optional[dict[str, Any]]:
    try:
        cid = str(uuid.UUID(str(campaign_id).strip()))
    except (ValueError, AttributeError):
        return None
    try:
        c = brand_camp.get_campaign(cid)
    except Exception as exc:
        raise RetailUnavailable(f"campaign lookup failed: {type(exc).__name__}") from exc
    return c if c and vocab.value_key(c.get("brand_name")) == scope.brand_key else None


def _metrics() -> dict[str, Any]:
    if _metrics_loader is None:
        raise RetailUnavailable("metrics loader not set")
    try:
        return _metrics_loader()
    except Exception as exc:
        raise RetailUnavailable(f"geo metrics unavailable: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def session(request: Request, authorization: Optional[str] = Header(None),
            x_session_token: Optional[str] = Header(None, alias="X-Session-Token")) -> dict[str, Any]:
    token = _token_resolver(request, authorization, x_session_token) if _token_resolver else None
    sess = auth_acl.get_session(token)
    if not sess:
        raise HTTPException(401, "You are not signed in. Please sign in again.")
    return sess


def scope_for(sess: dict[str, Any], brand: Optional[str]) -> Scope:
    role = str(sess.get("role") or "")
    if role not in RETAIL_ROLES:
        raise HTTPException(403, "The retail suite is for brand, warehouse and store logins.")
    if not db.configured():
        raise RetailUnavailable("DATABASE_URL not set")
    if role == "admin":
        name = (brand or "").strip()
        if not name:
            raise HTTPException(400, "Pick a brand to work on.")
    else:
        name = str(sess.get("brand_name") or "").strip()
        if not name:
            raise HTTPException(403, "This login has no brand assigned.")
    key = vocab.value_key(name)
    if not key or len(name) > 80:
        raise HTTPException(400, "Unknown brand.")
    if role not in STAFF_ROLES:
        return Scope(role, str(sess.get("username") or ""), key, name)
    code = str(sess.get("location_code") or "").strip().upper()
    with db.tx() as t:
        loc = stock.location_by_code(t, key, code)
    want = "warehouse" if role == "warehouse_staff" else "store"
    if not loc or not loc["active"] or loc["kind"] != want:
        raise HTTPException(403, f"Your login's {want} ({code or 'none'}) isn't open in the retail suite. "
                                 "Ask your brand admin.")
    return Scope(role, str(sess.get("username") or ""), key, name, loc["location_id"], loc["code"], loc["kind"])


def scope(sess: dict[str, Any] = Depends(session), brand: Optional[str] = Query(None, max_length=80)) -> Scope:
    return scope_for(sess, brand)


def _csv(body: dict[str, Any]) -> str:
    text = body.get("csv")
    if not isinstance(text, str) or not text.strip():
        raise RetailError("Paste or upload the CSV.")
    if len(text) > MAX_CSV_CHARS:
        raise RetailError("CSV is too large (5 MB max). Split it into smaller files.")
    return text


Json = Body(default_factory=dict)


# ---------------------------------------------------------------------------
# Context, settings, locations
# ---------------------------------------------------------------------------


@router.get("/me")
def me(s: Scope = Depends(scope)):
    out = {"role": s.role, "username": s.username, "brand_name": s.brand_name, "brand_key": s.brand_key,
           "location": None, "can": {k: s.role in roles for k, roles in PERMISSIONS.items()},
           "states": [{"code": c, "name": n} for c, n in sorted(gst.STATES.items(), key=lambda x: x[1])],
           "adjust_reasons": stock.ADJUST_REASONS}
    with db.tx() as t:
        out["settings"] = stock.settings(t, s)
        if s.location_id:
            out["location"] = stock.location(t, s, s.location_id)
    return out


@router.get("/brands")
def brands(sess: dict[str, Any] = Depends(session)):
    """Admin brand picker: brands in the Geo data plus brands already set up in the retail suite."""
    if sess.get("role") != "admin":
        raise HTTPException(403, "Admins only.")
    names = {b for b in camp_svc.known_brands() if b and b != "Competitors"}
    if db.configured():
        with db.tx() as t:
            names |= {r["brand_name"] for r in t.all("SELECT brand_name FROM retail_brand_settings") if r["brand_name"]}
    return {"brands": sorted(names, key=str.lower)}


@router.get("/settings")
def get_settings(s: Scope = Depends(scope)):
    return stock.get_settings(s)


@router.put("/settings")
def put_settings(body: dict = Json, s: Scope = Depends(scope)):
    return stock.save_settings(s, body)


@router.get("/locations")
def locations(kind: Optional[str] = Query(None), include_inactive: bool = False, s: Scope = Depends(scope)):
    return {"locations": stock.list_locations(s, kind=kind, include_inactive=include_inactive)}


@router.post("/locations")
def create_location(body: dict = Json, s: Scope = Depends(scope)):
    return stock.create_location(s, body)


@router.patch("/locations/{location_id}")
def update_location(location_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return stock.update_location(s, location_id, body)


@router.get("/locations/{location_id}/bins")
def bins(location_id: str, s: Scope = Depends(scope)):
    return {"bins": stock.list_bins(s, location_id)}


@router.post("/locations/{location_id}/bins")
def create_bins(location_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return {"bins": stock.create_bins(s, location_id, body)}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/products")
def products(q: str = "", category: str = "", active: Optional[bool] = None, page: int = 1, size: int = 50,
             s: Scope = Depends(scope)):
    return catalog.list_products(s, q=q, category=category, active=active, page=page, size=size)


@router.post("/products")
def create_product(body: dict = Json, s: Scope = Depends(scope)):
    return catalog.create_product(s, body)


@router.get("/products/{product_id}")
def get_product(product_id: str, s: Scope = Depends(scope)):
    return catalog.get_product(s, product_id)


@router.patch("/products/{product_id}")
def update_product(product_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return catalog.update_product(s, product_id, body)


@router.post("/products/{product_id}/variants")
def add_variants(product_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return catalog.add_variants(s, product_id, body)


@router.patch("/variants/{variant_id}")
def update_variant(variant_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return catalog.update_variant(s, variant_id, body)


@router.get("/variants/search")
def search_variants(q: str = "", location_id: Optional[str] = None, limit: int = 20, s: Scope = Depends(scope)):
    return {"results": catalog.search_variants(s, q=q, location_id=s.default_location(location_id), limit=limit)}


@router.post("/catalog/import")
def import_catalog(body: dict = Json, s: Scope = Depends(scope)):
    return catalog.import_catalog_csv(s, _csv(body))


@router.post("/products/{product_id}/images")
async def set_product_image(product_id: str, color: str = Form(...),
                            file: UploadFile = File(...), s: Scope = Depends(scope)):
    data = await file.read()
    return catalog.set_product_image(
        s, product_id, color=color, data=data,
        content_type=file.content_type or "", filename=file.filename or "sku.jpg",
    )


@router.delete("/products/{product_id}/images")
def delete_product_image(product_id: str, color: str = Query(...), s: Scope = Depends(scope)):
    return catalog.delete_product_image(s, product_id, color=color)


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


@router.get("/stock")
def stock_rows(location_id: Optional[str] = None, q: str = "", category: str = "", low_only: bool = False,
               page: int = 1, size: int = 100, s: Scope = Depends(scope)):
    return stock.stock_view(s, location_id=location_id, q=q, category=category, low_only=low_only, page=page,
                            size=size)


@router.get("/stock/bins")
def stock_bins(location_id: str, variant_id: Optional[str] = None, bin_code: Optional[str] = None,
               s: Scope = Depends(scope)):
    return {"rows": stock.bin_stock(s, location_id, variant_id=variant_id, bin_code=bin_code)}


@router.get("/stock/moves")
def stock_moves(location_id: Optional[str] = None, q: str = "", reason: str = "", date_from: Optional[str] = None,
                date_to: Optional[str] = None, page: int = 1, size: int = 100, s: Scope = Depends(scope)):
    return stock.moves(s, location_id=location_id, q=q, reason=reason, date_from=date_from, date_to=date_to,
                       page=page, size=size)


@router.get("/stock/valuation")
def valuation(s: Scope = Depends(scope)):
    return stock.valuation(s)


@router.post("/stock/adjust")
def adjust(body: dict = Json, s: Scope = Depends(scope)):
    return stock.adjust(s, str(s.default_location(body.get("location_id")) or ""), body)


@router.post("/stock/count")
def count(body: dict = Json, s: Scope = Depends(scope)):
    return stock.count(s, str(s.default_location(body.get("location_id")) or ""), body)


@router.post("/stock/putaway")
def putaway(body: dict = Json, s: Scope = Depends(scope)):
    return stock.putaway(s, str(s.default_location(body.get("location_id")) or ""), body)


@router.post("/stock/par-levels")
def par_levels(body: dict = Json, s: Scope = Depends(scope)):
    return stock.set_par_levels(s, str(body.get("location_id") or ""), body)


@router.post("/stock/opening-import")
def opening_import(body: dict = Json, s: Scope = Depends(scope)):
    return stock.import_opening_csv(s, _csv(body))


# ---------------------------------------------------------------------------
# Purchasing, transfers, replenishment, opening plan
# ---------------------------------------------------------------------------


@router.get("/suppliers")
def suppliers(s: Scope = Depends(scope)):
    return {"suppliers": wms.list_suppliers(s)}


@router.post("/suppliers")
def create_supplier(body: dict = Json, s: Scope = Depends(scope)):
    return wms.create_supplier(s, body)


@router.get("/purchase-orders")
def purchase_orders(status: str = "", page: int = 1, size: int = 50, s: Scope = Depends(scope)):
    return wms.list_pos(s, status=status, page=page, size=size)


@router.post("/purchase-orders")
def create_po(body: dict = Json, s: Scope = Depends(scope)):
    return wms.create_po(s, body)


@router.get("/purchase-orders/{po_id}")
def get_po(po_id: str, s: Scope = Depends(scope)):
    return wms.get_po(s, po_id)


@router.post("/purchase-orders/{po_id}/receive")
def receive_po(po_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return wms.receive_po(s, po_id, body)


@router.post("/purchase-orders/{po_id}/{action}")
def po_action(po_id: str, action: str, s: Scope = Depends(scope)):
    if action not in ("order", "cancel", "close"):
        raise HTTPException(404, "Not found")
    return wms.set_po_status(s, po_id, action)


@router.get("/transfers")
def transfers(status: str = "", direction: str = "", page: int = 1, size: int = 50, s: Scope = Depends(scope)):
    return wms.list_transfers(s, status=status, direction=direction, page=page, size=size)


@router.post("/transfers")
def create_transfer(body: dict = Json, s: Scope = Depends(scope)):
    return wms.create_transfer(s, body)


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: str, s: Scope = Depends(scope)):
    return wms.get_transfer(s, transfer_id)


@router.post("/transfers/{transfer_id}/dispatch")
def dispatch_transfer(transfer_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return wms.dispatch_transfer(s, transfer_id, body)


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return wms.receive_transfer(s, transfer_id, body)


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: str, s: Scope = Depends(scope)):
    return wms.cancel_transfer(s, transfer_id)


@router.get("/replenishment")
def replenishment(warehouse_id: Optional[str] = None, store_id: Optional[str] = None, cover_days: int = 14,
                  lookback_days: int = 28, s: Scope = Depends(scope)):
    wh = warehouse_id or (s.location_id if s.role == "warehouse_staff" else None)
    return wms.replenishment(s, warehouse_id=str(wh or ""), store_id=store_id, cover_days=cover_days,
                             lookback_days=lookback_days)


@router.post("/replenishment")
def create_replenishment(body: dict = Json, s: Scope = Depends(scope)):
    return wms.create_replenishment(s, body)


@router.post("/opening-plan")
def opening_plan(body: dict = Json, s: Scope = Depends(scope)):
    return insights.opening_plan(s, _metrics(), pin_code=str(body.get("pin_code") or ""),
                                 units=body.get("units") or 0,
                                 source_location_id=str(body.get("source_location_id") or ""))


@router.post("/opening-plan/transfer")
def opening_transfer(body: dict = Json, s: Scope = Depends(scope)):
    return insights.create_opening_transfer(s, body)


def demand_metrics() -> Optional[dict[str, Any]]:
    """Geo metrics when they load; the Stock Pilot still plans from stock and sales without them."""
    try:
        return _metrics()
    except RetailUnavailable as exc:
        log.warning("stock pilot without wardrobe demand: %s", exc)
        return None


@router.get("/pilot")
def stock_pilot(s: Scope = Depends(scope)):
    return pilot.build(s, demand_metrics())


@router.post("/pilot/actions/{action_id}/apply")
def apply_pilot_action(action_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return pilot.apply(s, demand_metrics(), action_id[:40], body)


# ---------------------------------------------------------------------------
# POS
# ---------------------------------------------------------------------------


@router.post("/pos/quote")
def pos_quote(body: dict = Json, s: Scope = Depends(scope)):
    return pos.quote(s, body)


@router.post("/pos/sales")
def pos_sale(body: dict = Json, s: Scope = Depends(scope)):
    return pos.create_sale(s, body)


@router.get("/pos/invoices")
def pos_invoices(location_id: Optional[str] = None, kind: str = "", q: str = "", date_from: Optional[str] = None,
                 date_to: Optional[str] = None, page: int = 1, size: int = 50, s: Scope = Depends(scope)):
    return pos.list_invoices(s, location_id=location_id, kind=kind, q=q, date_from=date_from, date_to=date_to,
                             page=page, size=size)


@router.get("/pos/invoices/by-number/{number}")
def pos_invoice_by_number(number: str, s: Scope = Depends(scope)):
    return pos.find_invoice(s, number)


@router.get("/pos/invoices/{invoice_id}")
def pos_invoice(invoice_id: str, s: Scope = Depends(scope)):
    return pos.get_invoice(s, invoice_id)


@router.post("/pos/invoices/{invoice_id}/returns")
def pos_return(invoice_id: str, body: dict = Json, s: Scope = Depends(scope)):
    return pos.create_return(s, invoice_id, body)


@router.get("/pos/day-report")
def pos_day_report(location_id: Optional[str] = None, day: Optional[str] = None, s: Scope = Depends(scope)):
    return pos.day_report(s, location_id=location_id, day=day)


@router.get("/pos/campaigns")
def pos_campaigns(location_id: Optional[str] = None, s: Scope = Depends(scope)):
    return {"campaigns": pos.running_campaigns(s, location_id=location_id)}


# ---------------------------------------------------------------------------
# Reports and links
# ---------------------------------------------------------------------------


@router.get("/sales-summary")
def sales_summary(date_from: Optional[str] = None, date_to: Optional[str] = None, location_id: Optional[str] = None,
                  s: Scope = Depends(scope)):
    return pos.sales_summary(s, date_from=date_from, date_to=date_to, location_id=location_id)


@router.get("/snapshot")
def snapshot(s: Scope = Depends(scope)):
    return insights.snapshot(s)


@router.get("/campaigns/{campaign_id}")
def campaign_retail(campaign_id: str, sess: dict[str, Any] = Depends(session),
                    brand: Optional[str] = Query(None, max_length=80)):
    """Stock at the campaign's stores and actual POS sales in its window (Campaigns tab card)."""
    try:
        campaign = camp_svc.load_campaign(sess, campaign_id)
        skus = brand_camp.list_campaign_skus(campaign["campaign_id"])
    except camp_svc.ServiceError as exc:
        if exc.status >= 500:
            raise RetailUnavailable(f"campaign lookup failed: {exc.code}") from exc
        raise HTTPException(exc.status, exc.public) from None
    except Exception as exc:
        raise RetailUnavailable(f"campaign skus failed: {type(exc).__name__}") from exc
    s = scope_for(sess, brand or campaign.get("brand_name"))
    if vocab.value_key(campaign.get("brand_name")) != s.brand_key:
        raise HTTPException(403, "Not allowed for this campaign")
    return insights.campaign_check(s, campaign, skus)


# ---------------------------------------------------------------------------
# Staff logins
# ---------------------------------------------------------------------------


@router.get("/staff")
def staff_list(s: Scope = Depends(scope)):
    return {"staff": staff.list_staff(s)}


@router.post("/staff")
def staff_create(body: dict = Json, s: Scope = Depends(scope)):
    return staff.create_staff(s, body)


@router.post("/staff/{username}/active")
def staff_active(username: str, body: dict = Json, s: Scope = Depends(scope)):
    return staff.set_active(s, username, bool(body.get("active")))


@router.post("/staff/{username}/reset-password")
def staff_reset(username: str, s: Scope = Depends(scope)):
    return staff.reset_password(s, username)
