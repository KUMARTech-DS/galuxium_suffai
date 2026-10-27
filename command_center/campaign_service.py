"""Brand campaign business logic shared by the Ads Lab REST routes and the AURA agent.

Every entry point takes the caller's session and enforces role + brand ownership, so
REST and AURA cannot diverge. Failures raise ServiceError with a stable code; the
`public` text is safe for the Ads Lab UI, while AURA maps codes to masked messages.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import admin_intel
import brand_campaign as brand_camp
import pin_garments

log = logging.getLogger("command_center.campaign_service")

MAX_REST_IMAGE_BYTES = 10 * 1024 * 1024
CONFIGURABLE_STATUSES = {"ranked", "configured", "exported"}
METRICS_CACHE_SEC = int(os.getenv("CAMPAIGN_METRICS_CACHE_SEC", "300"))


class ServiceError(Exception):
    def __init__(self, code: str, public: str, status: int = 400):
        super().__init__(public)
        self.code = code
        self.public = public
        self.status = status


@dataclass
class ImageInput:
    filename: str
    content_type: str
    data: bytes


_metrics_loader: Optional[Callable[[], dict[str, Any]]] = None
_metrics_cache: dict[str, Any] = {"at": 0.0, "data": None}
_metrics_lock = threading.Lock()


def init(*, metrics_loader: Callable[[], dict[str, Any]]) -> None:
    global _metrics_loader
    _metrics_loader = metrics_loader


def clear_metrics_cache() -> None:
    with _metrics_lock:
        _metrics_cache.update(at=0.0, data=None)


def load_metrics(*, fresh: bool = False) -> dict[str, Any]:
    """Geo metrics from BigQuery via the registered loader (cached). No file fallback: 503 when it fails."""
    now = time.time()
    with _metrics_lock:
        cached = _metrics_cache.get("data")
        if cached is not None and not fresh and now - _metrics_cache["at"] < METRICS_CACHE_SEC:
            return cached
    data: Optional[dict[str, Any]] = None
    if _metrics_loader is not None:
        try:
            data = _metrics_loader()
        except Exception:
            log.warning("metrics loader failed", exc_info=True)
    if data is None:
        raise ServiceError("unavailable", "Geo metrics unavailable", 503)
    with _metrics_lock:
        _metrics_cache.update(at=now, data=data)
    return data


def known_brands() -> list[str]:
    try:
        metrics = load_metrics()
    except ServiceError:
        return []
    filters = metrics.get("filters") if isinstance(metrics.get("filters"), dict) else {}
    brands = filters.get("brands") or []
    return [str(b) for b in brands if b]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def require_campaign_role(sess: Optional[dict[str, Any]]) -> str:
    if not sess:
        raise ServiceError("auth", "You are not signed in. Please sign in again.", 401)
    role = str(sess.get("role") or "")
    if role not in {"brand", "admin"}:
        raise ServiceError("not_allowed", "Only brand or admin can manage campaigns", 403)
    if role == "brand" and not str(sess.get("brand_name") or "").strip():
        raise ServiceError("not_allowed", "This brand account has no brand name assigned", 403)
    return role


def check_access(campaign: dict[str, Any], sess: Optional[dict[str, Any]]) -> None:
    role = require_campaign_role(sess)
    if role == "admin":
        return
    own = str(sess.get("brand_name") or "").strip().lower()  # type: ignore[union-attr]
    if own and own == str(campaign.get("brand_name") or "").strip().lower():
        return
    raise ServiceError("not_allowed", "Not allowed for this campaign", 403)


def _require_db() -> None:
    if not os.getenv("DATABASE_URL", "").strip():
        raise ServiceError("unavailable", "Campaign storage is not configured", 503)


def load_campaign(sess: Optional[dict[str, Any]], campaign_id: str) -> dict[str, Any]:
    require_campaign_role(sess)
    _require_db()
    try:
        cid = str(uuid.UUID(str(campaign_id).strip()))
    except (ValueError, AttributeError):
        raise ServiceError("not_found", "Campaign not found", 404) from None
    try:
        campaign = brand_camp.get_campaign(cid)
    except Exception as exc:
        log.exception("campaign lookup failed id=%s", cid)
        raise ServiceError("unavailable", "Campaign lookup failed", 503) from exc
    if not campaign:
        raise ServiceError("not_found", "Campaign not found", 404)
    check_access(campaign, sess)
    return campaign


def campaign_audience(campaign: dict[str, Any]) -> int:
    try:
        metrics = load_metrics()
    except ServiceError:
        return 0
    return brand_camp.audience_size(
        metrics,
        pin_codes=campaign.get("pin_codes") or [],
        age_group=campaign.get("age_group"),
        gender=campaign.get("gender"),
        rfm_segment=campaign.get("rfm_segment"),
    )


def _fee_features(campaign: dict[str, Any], *, audience: int, channels: Sequence[str],
                  objective: Optional[str], duration_days: int, media_budget_inr: float) -> dict[str, Any]:
    """Build aggregate-only inputs for the cold-start fee policy and future model labels."""
    try:
        skus = brand_camp.list_campaign_skus(campaign["campaign_id"])
        stats = brand_camp.campaign_fee_job_stats(campaign["campaign_id"])
    except Exception:
        # A fee recommendation must not make an otherwise valid campaign unusable
        # during the 014 rollout. The model still prices known aggregate campaign data.
        log.warning("campaign fee job stats unavailable id=%s", campaign.get("campaign_id"), exc_info=True)
        skus, stats = [], {}
    return {
        "audience": max(0, int(audience or 0)),
        "sku_count": len(skus),
        "pin_count": len(campaign.get("pin_codes") or []),
        "comparisons": int(stats.get("comparisons") or 0),
        "process_seconds": float(stats.get("process_seconds") or 0),
        "channels": list(channels),
        "objective": objective or campaign.get("objective"),
        "duration_days": int(duration_days or 1),
        "media_budget_inr": max(0.0, float(media_budget_inr or 0)),
    }


def _audit_fee_bq(campaign: dict[str, Any], rec: dict[str, Any], *, event_type: str,
                  final_fee_inr: Optional[float], actor_role: Optional[str]) -> None:
    """Best-effort aggregate audit; Cloud SQL remains the transactional source of truth."""
    project = (os.getenv("BQ_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    dataset = (os.getenv("BQ_DATASET") or "suffai_ops").strip()
    if not project or os.getenv("USE_BQ", "true").lower() == "false":
        return
    try:
        from google.cloud import bigquery  # type: ignore
        features = rec.get("features") or {}
        row = {
            "event_id": str(uuid.uuid4()), "campaign_id": str(campaign["campaign_id"]),
            "brand_name": str(campaign.get("brand_name") or ""), "event_type": event_type,
            "model_version": rec.get("model_version"), "source": rec.get("source"),
            "recommended_fee_inr": rec.get("recommended"),
            "final_fee_inr": final_fee_inr, "media_budget_inr": rec.get("media_budget_inr"),
            "total_checkout_inr": (rec.get("media_budget_inr") or 0) + (final_fee_inr if final_fee_inr is not None else rec.get("recommended") or 0),
            "audience_count": features.get("audience_count"), "sku_count": features.get("sku_count"),
            "pin_count": features.get("pin_count"), "comparisons": features.get("comparisons"),
            "process_seconds": features.get("process_seconds"), "channels": ",".join(features.get("channels") or []),
            "objective": features.get("objective"), "duration_days": features.get("duration_days"),
            "features_json": json.dumps(features, sort_keys=True), "actor_role": actor_role,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        errors = bigquery.Client(project=project).insert_rows_json(
            f"{project}.{dataset}.campaign_fee_recommendations", [row]
        )
        if errors:
            log.warning("campaign fee BQ audit insert failed: %s", errors)
    except Exception:
        log.warning("campaign fee BQ audit unavailable", exc_info=True)


def fee_recommendation(
    sess: Optional[dict[str, Any]], campaign_id: str, *, objective: Optional[str],
    duration_days: int, channels: Optional[Sequence[str]], media_budget_inr: Optional[float] = None,
    audit: bool = True,
) -> dict[str, Any]:
    campaign = load_campaign(sess, campaign_id)
    audience = campaign.get("est_audience")
    if audience is None:
        audience = campaign_audience(campaign)
    chans = brand_camp.normalize_channels(channels) if channels else (campaign.get("channels") or ["in_app"])
    days = duration_days or campaign.get("duration_days") or 14
    obj = objective or campaign.get("objective")
    media = float(media_budget_inr or 0)
    if media <= 0:
        media = brand_camp.recommend_budget(audience, days, obj, chans)["recommended"]
    features = _fee_features(
        campaign, audience=audience, channels=chans, objective=obj, duration_days=days, media_budget_inr=media,
    )
    rec = brand_camp.recommend_platform_fee(**features)
    # Vertex is intentionally shadow-only until the documented label threshold is met.
    from campaign_fee_model import shadow_prediction
    shadow = shadow_prediction(rec["features"])
    if shadow is not None:
        rec["vertex_shadow_fee_inr"] = round(max(0.0, shadow), 2)
    log.info(
        "campaign_fee_recommendation campaign_id=%s model=%s fee_inr=%.2f audience=%s sku_count=%s comparisons=%s shadow=%s",
        campaign["campaign_id"], rec["model_version"], rec["recommended"], rec["features"]["audience_count"],
        rec["features"]["sku_count"], rec["features"]["comparisons"], rec.get("vertex_shadow_fee_inr"),
    )
    log.info(
        "campaign_fee_recommendation campaign_id=%s model=%s fee_inr=%.2f audience=%s sku_count=%s comparisons=%s shadow=%s",
        campaign["campaign_id"], rec["model_version"], rec["recommended"], rec["features"]["audience_count"],
        rec["features"]["sku_count"], rec["features"]["comparisons"], rec.get("vertex_shadow_fee_inr"),
    )
    if audit:
        try:
            brand_camp.record_fee_decision(
                campaign["campaign_id"], brand_name=campaign["brand_name"], event_type="recommended",
                model_version=rec["model_version"], source=rec["source"], recommended_fee_inr=rec["recommended"],
                final_fee_inr=None, media_budget_inr=media, features=rec["features"],
                actor_role=str((sess or {}).get("role") or ""),
            )
        except Exception:
            log.warning("campaign fee Cloud SQL audit unavailable", exc_info=True)
        _audit_fee_bq(campaign, rec, event_type="recommended", final_fee_inr=None,
                      actor_role=str((sess or {}).get("role") or ""))
    return rec


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_for(
    sess: Optional[dict[str, Any]],
    *,
    status: Optional[str] = None,
    limit: int = 50,
    brand_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    role = require_campaign_role(sess)
    _require_db()
    brand = str(sess.get("brand_name") or "").strip() if role == "brand" else (brand_name or None)  # type: ignore[union-attr]
    try:
        return brand_camp.list_campaigns(brand_name=brand, status=status, limit=limit)
    except Exception as exc:
        log.exception("list campaigns failed")
        raise ServiceError("unavailable", "Campaign list unavailable", 503) from exc


def get_with_skus(
    sess: Optional[dict[str, Any]], campaign_id: str, *, with_images: bool = False
) -> dict[str, Any]:
    """Campaign + SKUs (created order) annotated with average PIN fit and fit rank (+ signed image URLs)."""
    campaign = load_campaign(sess, campaign_id)
    try:
        skus = brand_camp.list_campaign_skus(campaign["campaign_id"])
        fits = brand_camp.sku_pin_fit(campaign["campaign_id"])
    except Exception as exc:
        log.exception("campaign skus failed id=%s", campaign["campaign_id"])
        raise ServiceError("unavailable", "Campaign SKUs unavailable", 503) from exc
    for s in skus:
        s["pin_fit"] = round(fits.get(s["sku_uuid"], 0.0), 4)
    order = sorted(
        skus,
        key=lambda s: (
            not s.get("extract_ok"),
            -(s["rank_score"] if s.get("rank_score") is not None else s["pin_fit"]),
        ),
    )
    for i, s in enumerate(order, start=1):
        s["rank"] = i
    if with_images:
        import style_assistant as style_asst

        style_asst.attach_image_urls(skus)
    campaign["sku_count"] = len(skus)
    return {"campaign": campaign, "skus": skus}


def monitor_payload(
    sess: Optional[dict[str, Any]], campaign_id: str, *, with_images: bool = True
) -> dict[str, Any]:
    campaign = load_campaign(sess, campaign_id)
    cid = campaign["campaign_id"]
    try:
        skus = brand_camp.list_campaign_skus(cid)
        fits = brand_camp.sku_pin_fit(cid)
        daily = brand_camp.split_impact_daily(brand_camp.list_impact_daily(cid))
        channel_daily = brand_camp.list_impact_daily(cid, by_channel=True)
    except Exception as exc:
        log.exception("campaign monitor failed id=%s", cid)
        raise ServiceError("unavailable", "Campaign monitor unavailable", 503) from exc
    selected = set(campaign.get("selected_sku_ids") or [])
    if selected:
        skus = [s for s in skus if s["sku_uuid"] in selected]
    for s in skus:
        s["pin_fit"] = round(fits.get(s["sku_uuid"], 0.0), 4)
    skus.sort(key=lambda s: -s["pin_fit"])
    if with_images:
        import style_assistant as style_asst

        style_asst.attach_image_urls(skus)
    return {
        "ok": True,
        "simulated": True,
        "campaign": campaign,
        "skus": skus,
        "daily": daily,
        "channel_daily": channel_daily,
        "channels": campaign.get("channels") or ["in_app"],
        "insights": brand_camp.monitor_insights(campaign, daily["delivered"], daily["projected"]),
        "benchmark": brand_camp.objective_benchmark(campaign.get("objective")),
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def configure(
    sess: Optional[dict[str, Any]],
    campaign_id: str,
    *,
    campaign_name: str,
    objective: str,
    budget_inr: float,
    duration_days: int,
    start_date: Optional[str],
    selected_sku_ids: Sequence[str],
    creative: Optional[dict[str, Any]],
    source_context: Optional[dict[str, Any]] = None,
    channels: Optional[Sequence[str]] = None,
    message_template: Optional[dict[str, Any]] = None,
    platform_fee_inr: Optional[float] = None,
) -> dict[str, Any]:
    campaign = load_campaign(sess, campaign_id)
    if campaign.get("status") not in CONFIGURABLE_STATUSES:
        raise ServiceError(
            "invalid_input",
            f"Campaign must be ranked before it can be configured (status={campaign.get('status')})",
            409,
        )
    if not str((creative or {}).get("headline") or "").strip():
        raise ServiceError("invalid_input", "headline is required", 400)
    audience = campaign.get("est_audience")
    if audience is None:
        audience = campaign_audience(campaign)
    chans = brand_camp.normalize_channels(channels) if channels is not None else (campaign.get("channels") or ["in_app"])
    rec = brand_camp.recommend_budget(audience, duration_days, objective, chans)
    fee = fee_recommendation(
        sess, campaign_id, objective=objective, duration_days=duration_days, channels=chans,
        media_budget_inr=budget_inr, audit=False,
    )
    final_fee = float(platform_fee_inr) if platform_fee_inr is not None else float(fee["recommended"])
    if final_fee < float(fee["min"]) or final_fee > float(fee["max"]):
        raise ServiceError("invalid_input", "platform_fee_inr must be within the recommended fee range", 400)
    try:
        brand_camp.update_campaign_config(
            campaign["campaign_id"],
            campaign_name=campaign_name,
            objective=objective,
            budget_inr=budget_inr,
            duration_days=duration_days,
            start_date=start_date,
            selected_sku_ids=selected_sku_ids,
            creative=creative,
            source_context=source_context,
            est_audience=audience,
            channels=chans,
            message_template=message_template,
            budget_recommended=rec["recommended"],
            platform_fee_recommended=fee["recommended"],
            platform_fee_final=final_fee,
            fee_model_version_value=fee["model_version"],
            fee_features=fee["features"],
        )
        try:
            brand_camp.record_fee_decision(
                campaign["campaign_id"], brand_name=campaign["brand_name"], event_type="configured",
                model_version=fee["model_version"], source=fee["source"], recommended_fee_inr=fee["recommended"],
                final_fee_inr=final_fee, media_budget_inr=budget_inr, features=fee["features"],
                actor_role=str((sess or {}).get("role") or ""),
            )
        except Exception:
            log.warning("campaign fee Cloud SQL audit unavailable", exc_info=True)
        _audit_fee_bq(campaign, fee, event_type="configured", final_fee_inr=final_fee,
                      actor_role=str((sess or {}).get("role") or ""))
    except ValueError as exc:
        raise ServiceError("invalid_input", str(exc), 400) from exc
    except Exception as exc:
        log.exception("campaign config failed id=%s", campaign["campaign_id"])
        raise ServiceError("unavailable", "Campaign config failed", 503) from exc
    return brand_camp.get_campaign(campaign["campaign_id"]) or campaign


def estimate(
    sess: Optional[dict[str, Any]],
    campaign_id: str,
    *,
    objective: Optional[str],
    budget_inr: float,
    duration_days: int,
    channels: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    campaign = load_campaign(sess, campaign_id)
    audience = campaign.get("est_audience")
    if audience is None:
        audience = campaign_audience(campaign)
    chans = brand_camp.normalize_channels(channels) if channels else (campaign.get("channels") or ["in_app"])
    return brand_camp.estimate_outcomes(
        budget_inr, duration_days, audience, objective or campaign.get("objective"), chans
    )


def budget_recommendation(
    sess: Optional[dict[str, Any]],
    campaign_id: str,
    *,
    objective: Optional[str],
    duration_days: int,
    channels: Optional[Sequence[str]],
) -> dict[str, Any]:
    campaign = load_campaign(sess, campaign_id)
    audience = campaign.get("est_audience")
    if audience is None:
        audience = campaign_audience(campaign)
    chans = brand_camp.normalize_channels(channels) if channels else (campaign.get("channels") or ["in_app"])
    return brand_camp.recommend_budget(
        audience, duration_days or campaign.get("duration_days") or 14,
        objective or campaign.get("objective"), chans,
    )


def export_3p(sess: Optional[dict[str, Any]], campaign_id: str) -> dict[str, Any]:
    """Mark the campaign as run on another platform and return the ranked SKUs for CSV export."""
    detail = get_with_skus(sess, campaign_id)
    campaign = detail["campaign"]
    if campaign.get("status") not in CONFIGURABLE_STATUSES:
        raise ServiceError(
            "invalid_input",
            f"Only ranked campaigns can be exported (status={campaign.get('status')})",
            409,
        )
    try:
        brand_camp.mark_exported(campaign["campaign_id"])
    except Exception as exc:
        log.exception("campaign export failed id=%s", campaign["campaign_id"])
        raise ServiceError("unavailable", "Campaign export failed", 503) from exc
    rows = sorted(detail["skus"], key=lambda s: s.get("rank") or 1_000_000)
    items = []
    for s in rows:
        if s.get("process_status", "done") == "pending":
            continue
        attrs = s.get("attributes") or {}
        items.append({
            "rank": s.get("rank"),
            "filename": s.get("original_filename") or s.get("style_code"),
            "style_code": s.get("style_code"),
            "source": s.get("source") or "upload",
            "rank_score": s.get("rank_score"),
            "pin_fit": s.get("pin_fit"),
            **{k: attrs.get(k) for k in ("name", "color", "pattern", "material", "size")},
        })
    return {
        "campaign": brand_camp.get_campaign(campaign["campaign_id"]) or campaign,
        "audience": {
            "pin_codes": campaign.get("pin_codes") or [],
            "age_group": campaign.get("age_group"),
            "gender": campaign.get("gender"),
            "rfm_segment": campaign.get("rfm_segment"),
        },
        "items": items,
    }


def launch(sess: Optional[dict[str, Any]], campaign_id: str) -> dict[str, Any]:
    """Simulated launch: persist status + synthetic KPIs + daily series; nothing is delivered."""
    campaign = load_campaign(sess, campaign_id)
    if campaign.get("status") == "launched":
        return {"campaign": campaign, "impact": campaign.get("impact"), "already_launched": True}
    if campaign.get("status") != "configured":
        raise ServiceError(
            "invalid_input",
            f"Campaign must be configured before it can launch (status={campaign.get('status')})",
            409,
        )
    try:
        impact = brand_camp.finalize_synthetic_impact(campaign["campaign_id"])
        if campaign.get("platform_fee_recommended") is not None:
            try:
                brand_camp.record_fee_decision(
                    campaign["campaign_id"], brand_name=campaign["brand_name"], event_type="launched",
                    model_version=campaign.get("fee_model_version") or brand_camp.fee_model_version(),
                    source="cost_model_v1", recommended_fee_inr=float(campaign["platform_fee_recommended"]),
                    final_fee_inr=campaign.get("platform_fee_final"), media_budget_inr=campaign.get("budget_inr"),
                    features=campaign.get("fee_features") or {}, actor_role=str((sess or {}).get("role") or ""),
                )
            except Exception:
                log.warning("campaign launch fee audit unavailable", exc_info=True)
    except (LookupError, ValueError) as exc:
        raise ServiceError("invalid_input", str(exc), 400) from exc
    except Exception as exc:
        log.exception("campaign launch failed id=%s", campaign["campaign_id"])
        raise ServiceError("unavailable", "Campaign launch failed", 503) from exc
    return {
        "campaign": brand_camp.get_campaign(campaign["campaign_id"]) or campaign,
        "impact": impact,
        "already_launched": False,
    }


def discard(sess: Optional[dict[str, Any]], campaign_id: str) -> dict[str, Any]:
    campaign = load_campaign(sess, campaign_id)
    if campaign.get("status") == "processing":
        raise ServiceError("invalid_input", "Wait for the ranking job to finish before ignoring this campaign", 409)
    if campaign.get("status") in {"started", "launched"}:
        raise ServiceError(
            "invalid_input",
            f"A {campaign.get('status')} campaign can't be ignored. Contact an admin.",
            409,
        )
    try:
        brand_camp.set_campaign_status(campaign["campaign_id"], "ignored")
    except Exception as exc:
        log.exception("campaign discard failed id=%s", campaign["campaign_id"])
        raise ServiceError("unavailable", "Campaign update failed", 503) from exc
    return brand_camp.get_campaign(campaign["campaign_id"]) or campaign


# ---------------------------------------------------------------------------
# Upload → extract → compare-pins → rank pipeline
# ---------------------------------------------------------------------------


def _call_extraction(*, extraction_url: str, bucket: str, object_key: str, token: Optional[str]) -> dict[str, Any]:
    import urllib.request

    body = json.dumps(
        {
            "gcs_object_key": object_key,
            "gcs_bucket": bucket,
            "target_type": "garment",
            "idempotency_key": f"ads-lab-campaign-{uuid.uuid4().hex}",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{extraction_url}/v1/extract",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_compare_pins(
    *, similarity_url: str, bucket: str, object_key: str, pin_codes: list[str], token: Optional[str],
    corpus: dict[str, Any],
) -> dict[str, Any]:
    """corpus: pin_garments.corpus(), the garments the SKU is scored against (similarity adds embeddings)."""
    import urllib.request

    body = json.dumps(
        {"gcs_bucket": bucket, "gcs_object_key": object_key, "pin_codes": pin_codes, "topk": 5,
         "candidates": corpus.get("candidates") or {}, "pin_totals": corpus.get("pin_totals") or {}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{similarity_url.rstrip('/')}/v1/similarity/compare-pins",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_brand(sess: Optional[dict[str, Any]], requested: Optional[str]) -> str:
    """Brand sessions are always locked to their own brand; admins may name one."""
    require_campaign_role(sess)
    if sess and sess.get("role") == "brand":
        forced = str(sess.get("brand_name") or "").strip()
        if not forced:
            raise ServiceError("not_allowed", "This brand account has no brand name assigned", 403)
        return forced
    brand = (requested or "").strip()
    if not brand:
        raise ServiceError("invalid_input", "Brand name is required", 400)
    return brand


@dataclass
class Submission:
    brand: str
    pins: list[str]
    age: Optional[str]
    gender: Optional[str]
    rfm: Optional[str]
    images: list[ImageInput]


def validate_submission(
    sess: Optional[dict[str, Any]],
    images: Sequence[ImageInput],
    *,
    brand_name: Optional[str],
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
    max_image_bytes: int = MAX_REST_IMAGE_BYTES,
) -> Submission:
    brand = resolve_brand(sess, brand_name)
    pins = [str(p).strip() for p in pin_codes if str(p).strip()][:50]
    if not pins:
        raise ServiceError("invalid_input", "Select at least one PIN code", 400)
    usable = [img for img in images if img.content_type and img.content_type.startswith("image/")]
    if not usable:
        raise ServiceError("invalid_input", "At least one image file is required", 400)
    for img in usable:
        if len(img.data) > max_image_bytes:
            raise ServiceError(
                "invalid_input",
                f"{img.filename} too large (max {max_image_bytes // (1024 * 1024)}MB)",
                400,
            )
    return Submission(
        brand=brand,
        pins=pins,
        age=(age_group or "").strip() or None,
        gender=(gender or "").strip() or None,
        rfm=(rfm_segment or "").strip() or None,
        images=list(usable),
    )


@dataclass
class PipelineContext:
    brand: str
    pins: list[str]
    extraction_url: str
    similarity_url: str
    bucket: str
    bkt: Any
    profile: dict[str, Any]
    extract_token: Optional[str]
    sim_token: Optional[str]
    # pin_garments.corpus for `pins`: the photographed garments the Ads Lab tile counts.
    corpus: dict[str, Any] = field(default_factory=dict)


def garment_corpus(pins: Sequence[str]) -> dict[str, Any]:
    """Each PIN's photographed garments from BigQuery garment_items, as compare-pins candidates."""
    try:
        data = admin_intel.dataset()
    except admin_intel.IntelUnavailable as exc:
        raise ServiceError("unavailable", "Garment data unavailable", 503) from exc
    return pin_garments.corpus(data, list(pins))


def require_pipeline_env() -> tuple[str, str, str]:
    extraction_url = (os.getenv("EXTRACTION_URL") or "").rstrip("/")
    similarity_url = (os.getenv("SIMILARITY_URL") or "").rstrip("/")
    bucket = os.getenv("GCS_IMAGE_BUCKET") or ""
    if not extraction_url:
        raise ServiceError("unavailable", "Campaign processing is not configured (EXTRACTION_URL)", 503)
    if not similarity_url:
        raise ServiceError("unavailable", "Campaign processing is not configured (SIMILARITY_URL)", 503)
    if not bucket:
        raise ServiceError("unavailable", "Campaign processing is not configured (GCS_IMAGE_BUCKET)", 503)
    _require_db()
    return extraction_url, similarity_url, bucket


def gcs_bucket(bucket: str) -> Any:
    try:
        from google.cloud import storage  # type: ignore
    except ImportError as exc:
        log.exception("Missing GCP storage client")
        raise ServiceError("unavailable", "Image storage unavailable", 503) from exc
    try:
        return storage.Client().bucket(bucket)
    except Exception as exc:
        log.exception("GCS client init failed bucket=%s", bucket)
        raise ServiceError("unavailable", "Image storage unavailable", 503) from exc


def _id_token(audience: str) -> Optional[str]:
    try:
        import google.auth.transport.requests  # type: ignore
        import google.oauth2.id_token  # type: ignore

        return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
    except Exception:
        log.warning("Could not fetch ID token for %s", audience)
        return None


def pipeline_context(
    *,
    brand: str,
    pins: Sequence[str],
    age: Optional[str],
    gender: Optional[str],
    rfm: Optional[str],
    metrics: Optional[dict[str, Any]] = None,
) -> PipelineContext:
    extraction_url, similarity_url, bucket = require_pipeline_env()
    bkt = gcs_bucket(bucket)
    metrics = metrics if metrics is not None else load_metrics(fresh=True)
    profile = brand_camp.build_segment_profile(
        metrics, pin_codes=list(pins), age_group=age, gender=gender, rfm_segment=rfm
    )
    return PipelineContext(
        brand=brand,
        pins=list(pins),
        extraction_url=extraction_url,
        similarity_url=similarity_url,
        bucket=bucket,
        bkt=bkt,
        profile=profile,
        extract_token=_id_token(extraction_url),
        sim_token=_id_token(similarity_url),
        corpus=garment_corpus(pins),
    )


_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def upload_image(bkt: Any, *, brand: str, campaign_id: str, img: ImageInput) -> tuple[str, str]:
    """Upload to brands/{slug}/{campaign}/{sku}.{ext}; returns (sku_uuid, object_key)."""
    sku_uuid = str(uuid.uuid4())
    ext = _EXT.get(img.content_type, "jpg")
    object_key = f"brands/{brand_camp.brand_slug(brand)}/{campaign_id}/{sku_uuid}.{ext}"
    bkt.blob(object_key).upload_from_string(img.data, content_type=img.content_type)
    return sku_uuid, object_key


def extract_sku(pctx: PipelineContext, object_key: str) -> dict[str, Any]:
    try:
        payload = _call_extraction(
            extraction_url=pctx.extraction_url, bucket=pctx.bucket,
            object_key=object_key, token=pctx.extract_token,
        )
    except Exception as exc:
        log.exception("Extract failed key=%s", object_key)
        # Raw exception text (URLs, hosts) is logged above; brands only see a generic reason.
        return {"attributes": {"brand": pctx.brand}, "raw_result": {}, "extract_ok": False,
                "extract_error": "attribute extraction failed"}
    raw = payload.get("result") or {}
    attrs = extracted_attributes(payload, pctx.brand)
    if not any(attrs.get(k) for k in ("color", "name", "material", "pattern", "size")):
        log.warning("Extract returned no attributes key=%s status=%s", object_key, payload.get("status"))
    return {"attributes": attrs, "raw_result": raw, "extract_ok": True, "extract_error": None}


_UNKNOWN = {"", "unknown", "none", "null", "n/a", "other"}


def known_value(*values: Any) -> Optional[str]:
    for v in values:
        s = str(v).strip() if v is not None else ""
        if s.lower() not in _UNKNOWN:
            return s
    return None


def _evidence_confidence(raw: dict[str, Any]) -> Optional[float]:
    evidence = raw.get("attribute_evidence") if isinstance(raw.get("attribute_evidence"), dict) else {}
    confs = []
    for key in ("commodity_type", "material_type", "primary_color", "size_normalized"):
        item = evidence.get(key)
        if isinstance(item, dict) and item.get("confidence") is not None:
            try:
                confs.append(float(item["confidence"]))
            except (TypeError, ValueError):
                continue
    return min(confs) if confs else None


def extracted_attributes(payload: dict[str, Any], brand: Optional[str]) -> dict[str, Any]:
    """SKU attributes from a /v1/extract response, spelled like customer garments (T-Shirt, Navy Blue).

    item_attributes is worker/bq_items.item_attributes, the mapping behind suffai_ops.commodities;
    `brand` (the campaign's brand) wins over the brand read from the image.
    """
    raw = payload.get("result") or {}
    mapped = payload.get("item_attributes")
    if not isinstance(mapped, dict):
        log.warning("extraction response has no item_attributes (suffai-extraction older than Command Center)")
        mapped = {}
    confidence = _evidence_confidence(raw)
    if confidence is None:
        confidence = raw.get("confidence") or raw.get("confidence_score")
    out: dict[str, Any] = {k: known_value(mapped.get(k)) for k in
                           ("color", "size", "material", "pattern", "sleeve_type", "collar_type", "name")}
    out["brand"] = brand or known_value(mapped.get("brand"))
    out["confidence"] = confidence
    for tag in ("garment_category", "garment_subcategory", "sleeve_length"):
        value = known_value(raw.get(tag))
        if value:
            out[tag] = value
    pairs = [str(p) for p in raw.get("pairs_with") or [] if known_value(p)]
    if pairs:
        out["pairs_with"] = pairs
    return out


def compare_sku(
    pctx: PipelineContext, sku_uuid: str, object_key: str, attrs: dict[str, Any]
) -> dict[str, Any]:
    """compare-pins + rank blend; persists embedding and per-PIN scores for an existing SKU row."""
    sim_fit = 0.0
    freq_cov = 0.0
    per_pin: dict[str, Any] = {}
    compare_err = None
    try:
        cmp = _call_compare_pins(
            similarity_url=pctx.similarity_url, bucket=pctx.bucket, object_key=object_key,
            pin_codes=pctx.pins, token=pctx.sim_token, corpus=pctx.corpus,
        )
        sim_fit = float(cmp.get("aggregate_fit") or 0)
        per_pin = cmp.get("per_pin") or {}
        freq_cov = brand_camp.freq_coverage_from_compare(per_pin)
        emb = cmp.get("embedding")
        if isinstance(emb, list) and emb:
            brand_camp.update_sku_embedding(
                sku_uuid, emb, str(cmp.get("embedding_model") or ""),
                int(cmp.get("embedding_dim") or len(emb)),
            )
        brand_camp.upsert_pin_scores(sku_uuid, per_pin)
    except Exception as exc:
        log.exception("compare-pins failed sku=%s", sku_uuid)
        compare_err = "similarity comparison failed"
    # Size-independent ranking: size stays on the SKU row for display / 3P export only.
    rank_attrs = {**attrs, "size": None}
    a_fit = brand_camp.attr_fit_score(rank_attrs, pctx.profile)
    rank_score = brand_camp.blend_rank_score(sim_fit=sim_fit, freq_coverage=freq_cov, attr_fit=a_fit)
    return {
        "sim_fit": round(sim_fit, 4),
        "freq_coverage": round(freq_cov, 4),
        "attr_fit": round(a_fit, 4),
        "rank_score": rank_score,
        "compare_error": compare_err,
        "per_pin": per_pin,
    }


def process_sku(pctx: PipelineContext, *, sku_uuid: str, object_key: str,
                source: str = "upload", attributes: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Extract + compare + rank one pending SKU. Catalog rows skip re-extraction (trusted attrs)."""
    t0 = time.monotonic()
    if source == "catalog" and attributes:
        ext = {"attributes": {**attributes, "brand": pctx.brand}, "raw_result": {"source": "catalog"},
               "extract_ok": True, "extract_error": None}
    else:
        ext = extract_sku(pctx, object_key)
        brand_camp.update_sku_extract(
            sku_uuid,
            attributes=ext["attributes"],
            raw_result=ext["raw_result"],
            extract_ok=ext["extract_ok"],
            extract_error=ext["extract_error"],
        )
    cmp = compare_sku(pctx, sku_uuid, object_key, ext["attributes"])
    ok = ext["extract_ok"] and cmp["compare_error"] is None
    seconds = round(time.monotonic() - t0, 3)
    brand_camp.update_sku_rank(
        sku_uuid,
        rank_score=cmp["rank_score"],
        sim_fit=cmp["sim_fit"],
        attr_fit=cmp["attr_fit"],
        freq_coverage=cmp["freq_coverage"],
        compare_error=cmp["compare_error"],
        process_status="done" if ok else "failed",
        process_seconds=seconds,
    )
    return {"ok": ok, "seconds": seconds, "rank_score": cmp["rank_score"]}


def create_campaign_from_images(
    sess: Optional[dict[str, Any]],
    images: Sequence[ImageInput],
    *,
    brand_name: Optional[str],
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
    max_image_bytes: int = MAX_REST_IMAGE_BYTES,
) -> dict[str, Any]:
    """
    Synchronous brand SKU campaign (small uploads): GCS brands/{brand}/… → extract
    (brand overwritten) → compare-pins → rank with PIN + age/gender/RFM metrics profile.
    Large uploads go through campaign_jobs instead.
    """
    sub = validate_submission(
        sess, images, brand_name=brand_name, pin_codes=pin_codes, age_group=age_group,
        gender=gender, rfm_segment=rfm_segment, max_image_bytes=max_image_bytes,
    )
    pctx = pipeline_context(brand=sub.brand, pins=sub.pins, age=sub.age, gender=sub.gender, rfm=sub.rfm)

    try:
        campaign_id = brand_camp.create_campaign(
            brand_name=sub.brand, pin_codes=sub.pins, age_group=sub.age,
            gender=sub.gender, rfm_segment=sub.rfm,
        )
    except Exception as exc:
        log.exception("create_campaign failed")
        raise ServiceError("unavailable", "Campaign storage failed", 503) from exc

    ranked_items: list[dict[str, Any]] = []
    for img in sub.images:
        t0 = time.monotonic()
        try:
            sku_uuid, object_key = upload_image(pctx.bkt, brand=sub.brand, campaign_id=campaign_id, img=img)
        except Exception:
            log.exception("GCS upload failed campaign=%s", campaign_id)
            ranked_items.append(
                {"sku_uuid": None, "filename": img.filename, "ok": False,
                 "error": "Image upload failed", "rank_score": None}
            )
            continue

        ext = extract_sku(pctx, object_key)
        try:
            brand_camp.insert_sku(
                sku_uuid=sku_uuid,
                campaign_id=campaign_id,
                brand_name=sub.brand,
                original_filename=img.filename,
                gcs_bucket=pctx.bucket,
                gcs_object_key=object_key,
                attributes=ext["attributes"],
                raw_result=ext["raw_result"],
                extract_ok=ext["extract_ok"],
                extract_error=ext["extract_error"],
            )
        except Exception as exc:
            log.exception("insert_sku failed")
            raise ServiceError("unavailable", "SKU storage failed", 503) from exc

        cmp = compare_sku(pctx, sku_uuid, object_key, ext["attributes"])
        ok = ext["extract_ok"] and cmp["compare_error"] is None
        try:
            brand_camp.update_sku_rank(
                sku_uuid,
                rank_score=cmp["rank_score"],
                sim_fit=cmp["sim_fit"],
                attr_fit=cmp["attr_fit"],
                freq_coverage=cmp["freq_coverage"],
                compare_error=cmp["compare_error"],
                process_status="done" if ok else "failed",
                process_seconds=round(time.monotonic() - t0, 3),
            )
        except Exception:
            log.warning("update_sku_rank failed sku=%s", sku_uuid, exc_info=True)
        ranked_items.append(
            {
                "sku_uuid": sku_uuid,
                "filename": img.filename,
                "ok": ok,
                "gcs_object_key": object_key,
                "attributes": ext["attributes"],
                "extract_ok": ext["extract_ok"],
                "extract_error": ext["extract_error"],
                **cmp,
            }
        )

    ranked_items.sort(key=lambda r: (r.get("rank_score") is None, -(r.get("rank_score") or -1)))
    for i, row in enumerate(ranked_items, start=1):
        row["rank"] = i

    try:
        brand_camp.set_campaign_status(campaign_id, "ranked")
    except Exception:
        log.exception("set_campaign_status failed")

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "brand_name": sub.brand,
        "filters": {"pin_codes": sub.pins, "age_group": sub.age, "gender": sub.gender, "rfm_segment": sub.rfm},
        "profile_cell_count": pctx.profile.get("cell_count"),
        "items": ranked_items,
    }
