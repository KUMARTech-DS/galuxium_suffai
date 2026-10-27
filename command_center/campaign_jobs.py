"""Async extract & rank jobs for brand SKU campaigns.

submit_job uploads the images, stores them as pending SKUs and enqueues the job.
Workers process CAMPAIGN_JOB_CHUNK SKUs per invocation under a DB lease and enqueue a
continuation until no SKU is pending, so each Cloud Tasks request stays short and a
retried chunk never reprocesses finished SKUs.

Dispatch: Cloud Tasks (CLOUD_TASKS_QUEUE set) with an OIDC token for
TASKS_SERVICE_ACCOUNT, or an in-process background thread for local / single-instance use.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from typing import Any, Callable, Optional, Sequence

import brand_campaign as brand_camp
import campaign_service as svc
import style_assistant as style_asst
from campaign_service import ImageInput, ServiceError

log = logging.getLogger("command_center.campaign_jobs")

FINAL_STATUSES = ("succeeded", "failed")
# Stored as status 'failed' + this error_code (no schema change); reported to clients as status 'cancelled'.
CANCELLED = "cancelled"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def chunk_size() -> int:
    return max(1, min(50, _env_int("CAMPAIGN_JOB_CHUNK", 5)))


def lease_seconds() -> int:
    return max(60, _env_int("CAMPAIGN_JOB_LEASE_SEC", 900))


def max_job_images() -> int:
    return max(1, _env_int("CAMPAIGN_JOB_MAX_IMAGES", 500))


def max_catalog_skus() -> int:
    return max(1, min(15, _env_int("CAMPAIGN_CATALOG_MAX_SKUS", 15)))


# ---------------------------------------------------------------------------
# Counts + ETA
# ---------------------------------------------------------------------------


def seconds_per_image(total_garments: int) -> float:
    """Extraction + compare-pins cost per SKU; compare scales with the PIN garment corpus."""
    extract = _env_float("CAMPAIGN_ETA_EXTRACT_SEC", 8.0)
    compare = _env_float("CAMPAIGN_ETA_COMPARE_BASE_SEC", 4.0)
    per_1k = _env_float("CAMPAIGN_ETA_COMPARE_PER_1K_SEC", 1.5)
    return extract + compare + per_1k * max(0, total_garments) / 1000.0


def estimate_seconds(images: int, total_garments: int, *, per_image: Optional[float] = None) -> int:
    parallelism = max(1, _env_int("CAMPAIGN_JOB_PARALLELISM", 1))
    per = per_image if per_image and per_image > 0 else seconds_per_image(total_garments)
    return int(math.ceil(max(0, images) * per / parallelism))


def garments_in_pins(metrics: dict[str, Any], pins: Sequence[str]) -> int:
    """Photographed garments (all brands) in the selected PINs: the corpus each SKU is compared against.

    metrics pin_geo[pin].photo_garments counts distinct similarity garments at the customers' current PIN,
    capped like the compare-pins corpus (pin_garments.cap(), COMPARE_PIN_CANDIDATE_LIMIT per PIN).
    """
    wanted = {str(p).strip() for p in pins if str(p).strip()}
    geo = metrics.get("pin_geo") if isinstance(metrics.get("pin_geo"), dict) else {}
    total = 0
    for pin in wanted:
        row = geo.get(pin)
        if isinstance(row, dict):
            try:
                total += int(row.get("photo_garments") or 0)
            except (TypeError, ValueError):
                continue
    return total


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_JOB_COLS = """
    job_id::text, campaign_id::text, brand_name, status, total_images, processed_images,
    failed_images, total_customers, total_garments, comparisons, est_seconds, error_code,
    lease_until, created_at, started_at, finished_at, updated_at
"""


def _job_row(r: Sequence[Any]) -> dict[str, Any]:
    def ts(v: Any) -> Optional[str]:
        return v.isoformat() if v else None

    return {
        "job_id": r[0],
        "campaign_id": r[1],
        "brand_name": r[2],
        "status": r[3],
        "total_images": int(r[4] or 0),
        "processed_images": int(r[5] or 0),
        "failed_images": int(r[6] or 0),
        "total_customers": int(r[7] or 0),
        "total_garments": int(r[8] or 0),
        "comparisons": int(r[9] or 0),
        "est_seconds": int(r[10] or 0),
        "error_code": r[11],
        "lease_until": ts(r[12]),
        "created_at": ts(r[13]),
        "started_at": ts(r[14]),
        "finished_at": ts(r[15]),
        "updated_at": ts(r[16]),
    }


def _insert_job(job: dict[str, Any]) -> None:
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaign_jobs (
                  job_id, campaign_id, brand_name, status, total_images, failed_images,
                  total_customers, total_garments, comparisons, est_seconds
                ) VALUES (%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s)
                """,
                (
                    job["job_id"], job["campaign_id"], job["brand_name"], job["total_images"],
                    job.get("failed_images", 0), job["total_customers"], job["total_garments"],
                    job["comparisons"], job["est_seconds"],
                ),
            )


def fetch_job(job_id: str) -> Optional[dict[str, Any]]:
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_JOB_COLS} FROM campaign_jobs WHERE job_id = %s", (job_id,))
            r = cur.fetchone()
            return _job_row(r) if r else None


def _list_jobs(brand_name: Optional[str], *, active: bool, limit: int) -> list[dict[str, Any]]:
    where, params = [], []
    if brand_name:
        where.append("lower(brand_name) = lower(%s)")
        params.append(brand_name)
    if active:
        where.append("status IN ('queued', 'running')")
    sql = f"SELECT {_JOB_COLS} FROM campaign_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [_job_row(r) for r in cur.fetchall()]


def _lease(job_id: str) -> Optional[dict[str, Any]]:
    """Atomically take the job lease; None if finished or another worker holds it."""
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE campaign_jobs
                SET lease_until = now() + make_interval(secs => %s),
                    status = 'running',
                    started_at = COALESCE(started_at, now()),
                    updated_at = now()
                WHERE job_id = %s
                  AND status IN ('queued', 'running')
                  AND (lease_until IS NULL OR lease_until < now())
                RETURNING {_JOB_COLS}
                """,
                (lease_seconds(), job_id),
            )
            r = cur.fetchone()
            return _job_row(r) if r else None


def _release(job_id: str, progress: Optional[dict[str, Any]] = None) -> None:
    sets = ["lease_until = NULL", "updated_at = now()"]
    params: list[Any] = []
    if progress is not None:
        sets += ["processed_images = %s", "failed_images = %s"]
        params += [progress["done"] + progress["failed"], progress["failed"]]
    params.append(job_id)
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE campaign_jobs SET {', '.join(sets)} WHERE job_id = %s", tuple(params))


def _finish(job_id: str, status: str, *, error_code: Optional[str] = None,
            progress: Optional[dict[str, Any]] = None,
            campaign_status: Optional[str] = None, campaign_error: Optional[str] = None) -> bool:
    """Close an active job and, in the same transaction, set its campaign status.

    False (and nothing written) when the job already finished or was cancelled.
    """
    params: list[Any] = [status, error_code]
    extra = ""
    if progress is not None:
        extra = ", processed_images = %s, failed_images = %s"
        params += [progress["done"] + progress["failed"], progress["failed"]]
    params.append(job_id)
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE campaign_jobs
                SET status = %s, error_code = %s, lease_until = NULL,
                    finished_at = now(), updated_at = now(){extra}
                WHERE job_id = %s AND status IN ('queued', 'running')
                RETURNING campaign_id::text
                """,
                tuple(params),
            )
            r = cur.fetchone()
            if not r:
                return False
            if campaign_status:
                cur.execute(
                    "UPDATE brand_campaigns SET status = %s, error_message = %s, updated_at = now() WHERE campaign_id = %s",
                    (campaign_status, campaign_error, r[0]),
                )
            return True


def _is_active(job_id: str) -> bool:
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM campaign_jobs WHERE job_id = %s", (job_id,))
            r = cur.fetchone()
            return bool(r) and r[0] in ("queued", "running")


def _cancel(job_id: str, campaign_id: str) -> Optional[dict[str, Any]]:
    """Atomically stop an active job, drop its pending SKUs and discard its campaign; None if it already finished."""
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE campaign_jobs
                SET status = 'failed', error_code = %s, lease_until = NULL,
                    finished_at = now(), updated_at = now(),
                    processed_images = s.done + s.failed, failed_images = s.failed
                FROM (
                  SELECT COUNT(*) FILTER (WHERE process_status = 'done') AS done,
                         COUNT(*) FILTER (WHERE process_status = 'failed') AS failed
                  FROM brand_skus WHERE campaign_id = %s
                ) s
                WHERE job_id = %s AND status IN ('queued', 'running')
                RETURNING {_JOB_COLS}
                """,
                (CANCELLED, campaign_id, job_id),
            )
            r = cur.fetchone()
            if not r:
                return None
            cur.execute(
                """
                UPDATE brand_skus
                SET process_status = 'failed', compare_error = 'cancelled', processed_at = now()
                WHERE campaign_id = %s AND process_status = 'pending'
                """,
                (campaign_id,),
            )
            cur.execute(
                """
                UPDATE brand_campaigns
                SET status = 'ignored', error_message = 'Ranking cancelled',
                    ignored_at = COALESCE(ignored_at, now()), updated_at = now()
                WHERE campaign_id = %s
                """,
                (campaign_id,),
            )
            return _job_row(r)


def _mark_personalising(job_id: str, progress: dict[str, Any]) -> None:
    """All images ranked; keep the lease while per-customer picks are saved."""
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaign_jobs
                SET processed_images = %s, failed_images = %s,
                    lease_until = now() + make_interval(secs => %s), updated_at = now()
                WHERE job_id = %s
                """,
                (progress["done"] + progress["failed"], progress["failed"], lease_seconds(), job_id),
            )


def _personalise(job_id: str, campaign_id: str, progress: dict[str, Any]) -> None:
    """Save each eligible customer's Style Assistant picks; never blocks the ranking result."""
    try:
        _mark_personalising(job_id, progress)
        style_asst.personalise_campaign(campaign_id)
    except Exception:
        log.exception("personalise failed campaign=%s job=%s", campaign_id, job_id)


def _claim_requeue(job_id: str, stale_sec: int) -> bool:
    """Claim a stalled (unleased, untouched) job for re-dispatch; one caller wins."""
    with brand_camp.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaign_jobs SET updated_at = now()
                WHERE job_id = %s
                  AND status IN ('queued', 'running')
                  AND (lease_until IS NULL OR lease_until < now())
                  AND updated_at < now() - make_interval(secs => %s)
                RETURNING job_id
                """,
                (job_id, stale_sec),
            )
            return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return (os.getenv("COMMAND_CENTER_URL") or "").strip().rstrip("/")


def dispatch_mode() -> str:
    return "cloud_tasks" if (os.getenv("CLOUD_TASKS_QUEUE") or "").strip() else "thread"


def _enqueue_cloud_task(job_id: str, delay_sec: int = 0) -> None:
    from google.cloud import tasks_v2  # type: ignore
    from google.protobuf import duration_pb2, timestamp_pb2  # type: ignore

    queue = os.environ["CLOUD_TASKS_QUEUE"].strip()
    base = _base_url()
    sa = (os.getenv("TASKS_SERVICE_ACCOUNT") or "").strip()
    if not base or not sa:
        raise RuntimeError("COMMAND_CENTER_URL and TASKS_SERVICE_ACCOUNT are required for Cloud Tasks")
    task: dict[str, Any] = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{base}/internal/campaign-jobs/{job_id}/run",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"job_id": job_id}).encode("utf-8"),
            "oidc_token": {"service_account_email": sa, "audience": base},
        },
        "dispatch_deadline": duration_pb2.Duration(seconds=min(1800, lease_seconds())),
    }
    if delay_sec > 0:
        ts = timestamp_pb2.Timestamp()
        ts.GetCurrentTime()
        ts.seconds += delay_sec
        task["schedule_time"] = ts
    tasks_v2.CloudTasksClient().create_task(parent=queue, task=task)


def _run_locally(job_id: str) -> None:
    try:
        for _ in range(10_000):
            if not run_chunk(job_id, enqueue_next=False).get("continue"):
                return
    except Exception:
        log.exception("local campaign job crashed job=%s", job_id)


_enqueue_override: Optional[Callable[[str], None]] = None


def enqueue(job_id: str, *, delay_sec: int = 0) -> None:
    if _enqueue_override is not None:
        _enqueue_override(job_id)
        return
    if dispatch_mode() == "cloud_tasks":
        _enqueue_cloud_task(job_id, delay_sec)
        return
    threading.Thread(target=_run_locally, args=(job_id,), daemon=True, name=f"campaign-job-{job_id[:8]}").start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def job_view(job: dict[str, Any], progress: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    total = max(0, job["total_images"])
    processed = job["processed_images"]
    pending = max(0, total - processed)
    per_image = progress.get("avg_seconds") if progress else None
    cancelled = job["status"] == "failed" and job.get("error_code") == CANCELLED
    status = CANCELLED if cancelled else job["status"]
    if job["status"] in FINAL_STATUSES:
        eta, stage = 0, status
    elif total and pending == 0 and job["status"] == "running":
        eta, stage = _env_int("PERSONALISE_ETA_SEC", 30), "personalising"
    else:
        eta = estimate_seconds(pending, job["total_garments"], per_image=per_image)
        stage = "ranking" if job["status"] == "running" else job["status"]
    return {
        "job_id": job["job_id"],
        "campaign_id": job["campaign_id"],
        "brand_name": job["brand_name"],
        "status": status,
        "stage": stage,
        "total_images": total,
        "processed_images": processed,
        "failed_images": job["failed_images"],
        "total_customers": job["total_customers"],
        "total_garments": job["total_garments"],
        "comparisons": job["comparisons"],
        "est_seconds": job["est_seconds"],
        "eta_seconds": eta,
        "progress_pct": round(processed / total * 100, 1) if total else 0.0,
        "error": None if cancelled else svc_public_error(job.get("error_code")),
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }


_JOB_ERRORS = {
    "all_failed": "None of the images could be processed. Try clearer product photos.",
    "unavailable": "The ranking service is temporarily unavailable.",
    "not_found": "The campaign for this job no longer exists.",
    "enqueue_failed": "The job could not be scheduled.",
}


def svc_public_error(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return _JOB_ERRORS.get(code, "The job did not complete.")


def submit_job(
    sess: Optional[dict[str, Any]],
    images: Sequence[ImageInput],
    *,
    brand_name: Optional[str],
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
    max_image_bytes: int = svc.MAX_REST_IMAGE_BYTES,
) -> dict[str, Any]:
    sub = svc.validate_submission(
        sess, images, brand_name=brand_name, pin_codes=pin_codes, age_group=age_group,
        gender=gender, rfm_segment=rfm_segment, max_image_bytes=max_image_bytes,
    )
    if len(sub.images) > max_job_images():
        raise ServiceError("invalid_input", f"Too many images (max {max_job_images()} per job)", 400)
    _, _, bucket = svc.require_pipeline_env()
    bkt = svc.gcs_bucket(bucket)
    metrics = svc.load_metrics()
    customers = brand_camp.audience_size(
        metrics, pin_codes=sub.pins, age_group=sub.age, gender=sub.gender, rfm_segment=sub.rfm
    )
    garments = garments_in_pins(metrics, sub.pins)

    try:
        campaign_id = brand_camp.create_campaign(
            brand_name=sub.brand, pin_codes=sub.pins, age_group=sub.age,
            gender=sub.gender, rfm_segment=sub.rfm,
        )
        brand_camp.set_campaign_status(campaign_id, "processing")
    except Exception as exc:
        log.exception("create_campaign failed")
        raise ServiceError("unavailable", "Campaign storage failed", 503) from exc
    eligible = _eligible_count(campaign_id, {"pin_codes": sub.pins, "age_group": sub.age,
                                             "gender": sub.gender, "rfm_segment": sub.rfm})

    uploaded = 0
    for img in sub.images:
        try:
            sku_uuid, object_key = svc.upload_image(bkt, brand=sub.brand, campaign_id=campaign_id, img=img)
            brand_camp.insert_sku(
                sku_uuid=sku_uuid, campaign_id=campaign_id, brand_name=sub.brand,
                original_filename=img.filename, gcs_bucket=bucket, gcs_object_key=object_key,
                attributes={"brand": sub.brand}, raw_result=None, extract_ok=False,
                process_status="pending",
            )
            uploaded += 1
        except Exception:
            log.exception("job upload failed campaign=%s file=%s", campaign_id, img.filename)
    if uploaded == 0:
        brand_camp.set_campaign_status(campaign_id, "error", "Image upload failed")
        raise ServiceError("upload_failed", "Image upload failed", 503)

    job = {
        "job_id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "brand_name": sub.brand,
        "total_images": uploaded,
        "total_customers": customers,
        "total_garments": garments,
        "comparisons": uploaded * garments,
        "est_seconds": estimate_seconds(uploaded, garments),
    }
    try:
        _insert_job(job)
    except Exception as exc:
        log.exception("insert job failed campaign=%s", campaign_id)
        brand_camp.set_campaign_status(campaign_id, "error", "Job storage failed")
        raise ServiceError("unavailable", "Job storage failed", 503) from exc
    try:
        enqueue(job["job_id"])
    except Exception as exc:
        log.exception("enqueue failed job=%s", job["job_id"])
        _finish(job["job_id"], "failed", error_code="enqueue_failed")
        brand_camp.set_campaign_status(campaign_id, "error", "Job could not be scheduled")
        raise ServiceError("unavailable", "Ranking job could not be scheduled", 503) from exc
    out = job_view(fetch_job(job["job_id"]) or {**job, "status": "queued", "processed_images": 0,
                                                  "failed_images": 0, "created_at": None,
                                                  "started_at": None, "finished_at": None})
    out["skipped_images"] = len(sub.images) - uploaded
    out["dispatch"] = dispatch_mode()
    out["eligible_customers"] = eligible
    return out


def _eligible_count(campaign_id: str, audience: dict[str, Any]) -> Optional[int]:
    """Audience customers who will get Style Assistant picks; saved on the campaign for the job view."""
    try:
        n = len(style_asst.eligible_customers(audience))
        brand_camp.set_campaign_impact(campaign_id, eligible_users=n)
        return n
    except Exception:
        log.warning("eligible count unavailable campaign=%s", campaign_id, exc_info=True)
        return None


def submit_catalog_job(
    sess: Optional[dict[str, Any]],
    items: Sequence[dict[str, Any]],
    *,
    brand_name: Optional[str],
    pin_codes: Sequence[str],
    age_group: Optional[str],
    gender: Optional[str],
    rfm_segment: Optional[str],
) -> dict[str, Any]:
    """Rank up to 15 style × colour SKUs from the brand's warehouse catalog (size ignored)."""
    import retail_catalog
    import retail_db
    import vocab
    from retail_scope import Scope

    # Audience validation without requiring upload images.
    brand = svc.resolve_brand(sess, brand_name)
    pins = [str(p).strip() for p in pin_codes if str(p).strip()][:50]
    if not pins:
        raise ServiceError("invalid_input", "Select at least one PIN code", 400)
    age = (age_group or "").strip() or None
    gen = (gender or "").strip() or None
    rfm = (rfm_segment or "").strip() or None

    if not retail_db.configured():
        raise ServiceError("unavailable", "Warehouse catalog is not configured", 503)
    raw_items = list(items or [])
    if not raw_items:
        raise ServiceError("invalid_input", "Select at least one SKU from your catalog", 400)
    if len(raw_items) > max_catalog_skus():
        raise ServiceError("invalid_input", f"Select at most {max_catalog_skus()} SKUs", 400)

    scope = Scope(str(sess.get("role") or "brand"), str(sess.get("username") or ""),  # type: ignore[union-attr]
                  vocab.value_key(brand), brand)
    catalog = retail_catalog.list_style_colours(scope)
    by_key = {(it["style_code"].upper(), it["color_key"]): it for it in catalog["items"]}
    seen: set[tuple[str, str]] = set()
    resolved: list[dict[str, Any]] = []
    for raw in raw_items:
        style = str(raw.get("style_code") or "").strip().upper()
        color = str(raw.get("color") or "").strip()
        if not style or not color:
            raise ServiceError("invalid_input", "Each pick needs a style code and colour", 400)
        ckey = vocab.value_key(color)
        key = (style, ckey)
        if key in seen:
            raise ServiceError("invalid_input", f"Duplicate pick: {style} · {color}", 400)
        seen.add(key)
        hit = by_key.get(key)
        if not hit:
            raise ServiceError("invalid_input", f"{style} · {color} is not in your catalog", 404)
        resolved.append(hit)

    # Reload GCS keys for each resolved product × colour (not in public list payload).
    with retail_db.tx() as t:
        for hit in resolved:
            row = t.one(
                "SELECT gcs_bucket, gcs_object_key, color FROM retail_product_images "
                "WHERE product_id = %s AND color_key = %s",
                (hit["product_id"], hit["color_key"]),
            )
            if not row:
                raise ServiceError(
                    "invalid_input",
                    f"Add a photo for {hit['style_code']} · {hit['color']} in Inventory › Catalog first",
                    400,
                )
            hit["gcs_bucket"] = row["gcs_bucket"]
            hit["gcs_object_key"] = row["gcs_object_key"]
            hit["color"] = row["color"]

    _, _, bucket = svc.require_pipeline_env()
    metrics = svc.load_metrics()
    customers = brand_camp.audience_size(
        metrics, pin_codes=pins, age_group=age, gender=gen, rfm_segment=rfm
    )
    garments = garments_in_pins(metrics, pins)

    try:
        campaign_id = brand_camp.create_campaign(
            brand_name=brand, pin_codes=pins, age_group=age, gender=gen, rfm_segment=rfm,
        )
        brand_camp.set_campaign_status(campaign_id, "processing")
    except Exception as exc:
        log.exception("create_campaign failed (catalog)")
        raise ServiceError("unavailable", "Campaign storage failed", 503) from exc
    eligible = _eligible_count(campaign_id, {"pin_codes": pins, "age_group": age,
                                             "gender": gen, "rfm_segment": rfm})

    uploaded = 0
    for hit in resolved:
        sku_uuid = str(uuid.uuid4())
        sizes = "/".join(hit.get("sizes") or [])
        name = vocab.canonical("garment_name", hit.get("category") or hit.get("name") or "", default="") or hit.get("name")
        attrs = {
            "brand": brand,
            "color": hit["color"],
            "material": hit.get("material"),
            "pattern": hit.get("pattern"),
            "name": name,
            "commodity": name,
            "size": sizes or None,
            "confidence": 1.0,
        }
        try:
            brand_camp.insert_sku(
                sku_uuid=sku_uuid, campaign_id=campaign_id, brand_name=brand,
                original_filename=f"{hit['style_code']}-{hit['color']}",
                gcs_bucket=hit["gcs_bucket"], gcs_object_key=hit["gcs_object_key"],
                attributes=attrs, raw_result={"source": "catalog", "style_code": hit["style_code"]},
                extract_ok=True, process_status="pending", source="catalog",
                retail_product_id=hit["product_id"], style_code=hit["style_code"],
            )
            uploaded += 1
        except Exception:
            log.exception("catalog sku insert failed campaign=%s style=%s", campaign_id, hit["style_code"])
    if uploaded == 0:
        brand_camp.set_campaign_status(campaign_id, "error", "No catalog SKUs could be queued")
        raise ServiceError("unavailable", "Could not queue catalog SKUs", 503)

    job = {
        "job_id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "brand_name": brand,
        "total_images": uploaded,
        "total_customers": customers,
        "total_garments": garments,
        "comparisons": uploaded * garments,
        "est_seconds": estimate_seconds(uploaded, garments),
    }
    try:
        _insert_job(job)
    except Exception as exc:
        log.exception("insert catalog job failed campaign=%s", campaign_id)
        brand_camp.set_campaign_status(campaign_id, "error", "Job storage failed")
        raise ServiceError("unavailable", "Job storage failed", 503) from exc
    try:
        enqueue(job["job_id"])
    except Exception as exc:
        log.exception("enqueue failed job=%s", job["job_id"])
        _finish(job["job_id"], "failed", error_code="enqueue_failed")
        brand_camp.set_campaign_status(campaign_id, "error", "Job could not be scheduled")
        raise ServiceError("unavailable", "Ranking job could not be scheduled", 503) from exc
    out = job_view(fetch_job(job["job_id"]) or {**job, "status": "queued", "processed_images": 0,
                                                  "failed_images": 0, "created_at": None,
                                                  "started_at": None, "finished_at": None})
    out["skipped_images"] = 0
    out["dispatch"] = dispatch_mode()
    out["eligible_customers"] = eligible
    out["source"] = "catalog"
    return out


def _load_job_for(sess: Optional[dict[str, Any]], job_id: str) -> dict[str, Any]:
    svc.require_campaign_role(sess)
    svc._require_db()
    try:
        jid = str(uuid.UUID(str(job_id).strip()))
    except (ValueError, AttributeError):
        raise ServiceError("not_found", "Job not found", 404) from None
    try:
        job = fetch_job(jid)
    except Exception as exc:
        log.exception("job lookup failed id=%s", jid)
        raise ServiceError("unavailable", "Job lookup failed", 503) from exc
    if not job:
        raise ServiceError("not_found", "Job not found", 404)
    try:
        svc.check_access({"brand_name": job["brand_name"]}, sess)
    except ServiceError:
        raise ServiceError("not_found", "Job not found", 404) from None
    return job


def _maybe_recover(job: dict[str, Any]) -> None:
    """Re-dispatch a job whose worker died (instance restart, dropped task)."""
    if job["status"] in FINAL_STATUSES:
        return
    stale = max(120, _env_int("CAMPAIGN_JOB_STALE_SEC", 180))
    try:
        if _claim_requeue(job["job_id"], stale):
            log.warning("re-dispatching stalled job=%s", job["job_id"])
            enqueue(job["job_id"])
    except Exception:
        log.warning("stalled job recovery failed job=%s", job["job_id"], exc_info=True)


def cancel_job(sess: Optional[dict[str, Any]], job_id: str) -> dict[str, Any]:
    """Stop a queued / running job; the worker halts before its next image and the campaign is discarded."""
    job = _load_job_for(sess, job_id)
    if job["status"] in FINAL_STATUSES:
        raise ServiceError("invalid_input", "This ranking job has already finished", 409)
    try:
        stopped = _cancel(job["job_id"], job["campaign_id"])
    except Exception as exc:
        log.exception("cancel failed job=%s", job["job_id"])
        raise ServiceError("unavailable", "The ranking job could not be cancelled", 503) from exc
    if not stopped:
        raise ServiceError("invalid_input", "This ranking job has already finished", 409)
    log.info("campaign job cancelled job=%s campaign=%s", stopped["job_id"], stopped["campaign_id"])
    return job_view(stopped)


def get_job(sess: Optional[dict[str, Any]], job_id: str) -> dict[str, Any]:
    job = _load_job_for(sess, job_id)
    _maybe_recover(job)
    progress = None
    if job["status"] not in FINAL_STATUSES:
        try:
            progress = brand_camp.sku_progress(job["campaign_id"])
            job["processed_images"] = progress["done"] + progress["failed"]
            job["failed_images"] = progress["failed"]
        except Exception:
            log.warning("sku_progress failed job=%s", job["job_id"], exc_info=True)
    out = job_view(job, progress)
    try:
        campaign = brand_camp.get_campaign(job["campaign_id"]) or {}
    except Exception:
        log.warning("campaign lookup failed job=%s", job["job_id"], exc_info=True)
        campaign = {}
    out["eligible_customers"] = campaign.get("eligible_users")
    if job["status"] == "succeeded":
        out["campaign_name"] = campaign.get("campaign_name")
        out["campaign_status"] = campaign.get("status")
    return out


def list_jobs(sess: Optional[dict[str, Any]], *, active: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    role = svc.require_campaign_role(sess)
    svc._require_db()
    brand = str(sess.get("brand_name") or "").strip() if role == "brand" else None  # type: ignore[union-attr]
    try:
        jobs = _list_jobs(brand, active=active, limit=max(1, min(100, limit)))
    except Exception as exc:
        log.exception("list jobs failed")
        raise ServiceError("unavailable", "Job list unavailable", 503) from exc
    return [job_view(j) for j in jobs]


def run_chunk(job_id: str, *, enqueue_next: bool = True) -> dict[str, Any]:
    """Process up to chunk_size() pending SKUs. Idempotent and safe to retry."""
    job = _lease(job_id)
    if not job:
        return {"ok": True, "skipped": True}
    cid = job["campaign_id"]
    try:
        campaign = brand_camp.get_campaign(cid)
        if not campaign:
            _finish(job_id, "failed", error_code="not_found")
            return {"ok": True, "status": "failed"}
        pending = brand_camp.list_pending_skus(cid, limit=chunk_size())
        if pending:
            try:
                pctx = svc.pipeline_context(
                    brand=campaign["brand_name"], pins=campaign.get("pin_codes") or [],
                    age=campaign.get("age_group"), gender=campaign.get("gender"),
                    rfm=campaign.get("rfm_segment"),
                )
            except ServiceError:
                log.exception("pipeline unavailable job=%s", job_id)
                _finish(job_id, "failed", error_code="unavailable",
                        campaign_status="error", campaign_error="Ranking service unavailable")
                return {"ok": True, "status": "failed"}
            for sku in pending:
                if not _is_active(job_id):
                    log.info("campaign job stopped (cancelled) job=%s", job_id)
                    return {"ok": True, "status": CANCELLED}
                try:
                    svc.process_sku(
                        pctx, sku_uuid=sku["sku_uuid"], object_key=sku["gcs_object_key"],
                        source=sku.get("source") or "upload", attributes=sku.get("attributes"),
                    )
                except Exception:
                    log.exception("process_sku failed sku=%s", sku["sku_uuid"])
                    brand_camp.update_sku_rank(
                        sku["sku_uuid"], rank_score=None, sim_fit=0.0, attr_fit=0.0,
                        freq_coverage=0.0, compare_error="processing failed",
                        process_status="failed", process_seconds=None,
                    )
        progress = brand_camp.sku_progress(cid)
        if progress["pending"] > 0:
            _release(job_id, progress)
            if enqueue_next:
                enqueue(job_id)
            return {"ok": True, "continue": True, "progress": progress}
        if not _is_active(job_id):
            return {"ok": True, "status": CANCELLED, "progress": progress}
        if progress["done"] == 0:
            _finish(job_id, "failed", error_code="all_failed", progress=progress,
                    campaign_status="error", campaign_error="No images could be processed")
            return {"ok": True, "status": "failed", "progress": progress}
        _personalise(job_id, cid, progress)
        if not _finish(job_id, "succeeded", progress=progress, campaign_status="ranked"):
            return {"ok": True, "status": CANCELLED, "progress": progress}
        return {"ok": True, "status": "succeeded", "progress": progress}
    except Exception:
        try:
            _release(job_id)
        except Exception:
            log.warning("lease release failed job=%s", job_id, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Worker authentication (Cloud Tasks OIDC)
# ---------------------------------------------------------------------------


def _google_verify(token: str, audience: str) -> dict[str, Any]:
    from google.auth.transport import requests as g_requests  # type: ignore
    from google.oauth2 import id_token  # type: ignore

    return id_token.verify_oauth2_token(token, g_requests.Request(), audience=audience)


def verify_task_request(
    authorization: Optional[str],
    *,
    verifier: Optional[Callable[[str, str], dict[str, Any]]] = None,
) -> bool:
    """True only for a Google-signed OIDC token for COMMAND_CENTER_URL minted for TASKS_SERVICE_ACCOUNT."""
    sa = (os.getenv("TASKS_SERVICE_ACCOUNT") or "").strip().lower()
    audience = _base_url()
    if not sa or not audience:
        return False
    auth = (authorization or "").strip()
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    try:
        claims = (verifier or _google_verify)(token, audience)
    except Exception:
        return False
    email_ok = str(claims.get("email") or "").lower() == sa
    verified = claims.get("email_verified") in (True, "true")
    return bool(email_ok and verified)
