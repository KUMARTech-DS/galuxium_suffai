"""Operational garment / campaign store (JSON under data_derived; Cloud SQL optional later)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GarmentStore:
    COLLECTIONS = (
        "garment_instances",
        "garment_images",
        "consent_records",
        "attribute_corrections",
        "extraction_jobs",
        "brand_catalog_skus",
        "fashion_brands",
        "campaigns",
        "campaign_evaluations",
        "saved_views",
        "audit_events",
    )

    def __init__(self, derived_dir: str | Path):
        self.root = Path(derived_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in self.COLLECTIONS:
            path = self.root / f"{name}.json"
            if not path.exists():
                path.write_text("[]\n", encoding="utf-8")

    def _load(self, name: str) -> list[dict[str, Any]]:
        return json.loads((self.root / f"{name}.json").read_text(encoding="utf-8"))

    def _save(self, name: str, rows: list[dict[str, Any]]) -> None:
        (self.root / f"{name}.json").write_text(
            json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8"
        )
        # Dual-write to Cloud SQL ops mirror when DATABASE_URL is set
        try:
            from services.ops_db import configured, mirror_collection

            if configured():
                id_keys = (
                    "garment_instance_id",
                    "image_id",
                    "consent_id",
                    "job_id",
                    "campaign_id",
                    "evaluation_id",
                    "view_id",
                    "sku_id",
                    "correction_id",
                    "id",
                )
                mirror_collection(name, rows, id_keys=id_keys)
        except Exception:
            pass

    def audit(self, action: str, **detail: Any) -> None:
        rows = self._load("audit_events")
        rows.append({"id": str(uuid.uuid4()), "action": action, "detail": detail, "created_at": _now()})
        self._save("audit_events", rows[-5000:])  # cap

    # ----- consent -----
    def create_consent(
        self,
        source_customer_id: str,
        purpose: str,
        *,
        policy_version: str = "v1",
    ) -> dict[str, Any]:
        rows = self._load("consent_records")
        rec = {
            "consent_id": str(uuid.uuid4()),
            "id": None,  # alias
            "source_customer_id": source_customer_id,
            "purpose": purpose,
            "policy_version": policy_version,
            "status": "granted",
            "granted_at": _now(),
            "revoked_at": None,
            "created_at": _now(),
        }
        rec["id"] = rec["consent_id"]
        rows.append(rec)
        self._save("consent_records", rows)
        return rec

    def revoke_consent(self, consent_id: str) -> dict[str, Any]:
        rows = self._load("consent_records")
        found = None
        for r in rows:
            if r.get("consent_id") == consent_id or r.get("id") == consent_id:
                r["status"] = "revoked"
                r["revoked_at"] = _now()
                found = r
                break
        if not found:
            raise ValueError("consent not found")
        self._save("consent_records", rows)
        # soft-delete related images/garments
        images = self._load("garment_images")
        for img in images:
            if img.get("consent_id") == consent_id:
                img["deleted_at"] = _now()
                img["deletion_state"] = "revoked"
        self._save("garment_images", images)
        garments = self._load("garment_instances")
        for g in garments:
            if g.get("consent_id") == consent_id:
                g["deleted_at"] = _now()
                g["review_status"] = "revoked"
        self._save("garment_instances", garments)
        self.audit("consent_revoked", consent_id=consent_id)
        return found

    # ----- garments -----
    def list_garment_instances(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        rows = self._load("garment_instances")
        if include_deleted:
            return rows
        return [g for g in rows if not g.get("deleted_at")]

    def get_garment(self, garment_instance_id: str) -> Optional[dict[str, Any]]:
        for g in self.list_garment_instances(include_deleted=True):
            if g.get("garment_instance_id") == garment_instance_id or g.get("id") == garment_instance_id:
                return g
        return None

    def list_verified_brand_observations(self) -> list[dict[str, Any]]:
        return [
            g for g in self.list_garment_instances()
            if g.get("review_status") in {"completed", "COMPLETED"}
            and (g.get("brand_id") or (g.get("brand_name_raw") and g.get("brand_name_raw") != "unknown"))
            and not g.get("deleted_at")
        ]

    def register_garment_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = [
            "source_customer_id",
            "source_order_id",
            "source_order_item_id",
            "source_order_commodity_line_id",
            "physical_item_index",
            "consent_id",
        ]
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        if int(payload["physical_item_index"]) < 1:
            raise ValueError("physical_item_index must be >= 1")

        rows = self.list_garment_instances(include_deleted=True)
        key = (
            payload["source_order_id"],
            payload["source_order_commodity_line_id"],
            int(payload["physical_item_index"]),
        )
        for g in rows:
            existing = (
                g.get("source_order_id"),
                g.get("source_order_commodity_line_id"),
                int(g.get("physical_item_index") or 0),
            )
            if existing == key and not g.get("deleted_at"):
                raise ValueError("Duplicate garment instance for order line + index")

        gid = str(uuid.uuid4())
        rec = {
            "garment_instance_id": gid,
            "id": gid,
            "source_user_id": payload.get("source_user_id"),
            "bill_no": payload.get("bill_no"),
            "source_store_id": payload.get("source_store_id"),
            "source_service_id": payload.get("source_service_id"),
            "source_catalog_commodity_id": payload.get("source_catalog_commodity_id"),
            "order_pin_code": payload.get("order_pin_code"),
            "observed_at": payload.get("observed_at") or _now(),
            "commodity_type": payload.get("commodity_type") or "unknown",
            "material_type": payload.get("material_type") or "unknown",
            "primary_color": payload.get("primary_color") or "unknown",
            "secondary_colors": payload.get("secondary_colors") or [],
            "size_raw": payload.get("size_raw"),
            "size_normalized": payload.get("size_normalized"),
            "size_system": payload.get("size_system"),
            "brand_id": payload.get("brand_id"),
            "brand_name_raw": payload.get("brand_name_raw") or "unknown",
            "brand_sku": payload.get("brand_sku"),
            "pattern": payload.get("pattern") or "unknown",
            "style_tags": payload.get("style_tags") or [],
            "attribute_evidence": payload.get("attribute_evidence") or {},
            "review_status": payload.get("review_status") or "pending_upload",
            "model_version": payload.get("model_version"),
            "created_at": _now(),
            "updated_at": _now(),
            "deleted_at": None,
            **{k: payload[k] for k in required},
        }
        rows.append(rec)
        self._save("garment_instances", rows)
        self.audit("garment_registered", garment_instance_id=gid)
        return rec

    def register_image(
        self,
        *,
        garment_instance_id: str,
        consent_id: str,
        gcs_object_key: str,
        content_type: str = "image/jpeg",
        capture_source: str = "ops_upload",
        sha256: Optional[str] = None,
    ) -> dict[str, Any]:
        rows = self._load("garment_images")
        for r in rows:
            if r.get("gcs_object_key") == gcs_object_key and not r.get("deleted_at"):
                return r
        image_id = str(uuid.uuid4())
        rec = {
            "image_id": image_id,
            "id": image_id,
            "garment_instance_id": garment_instance_id,
            "consent_id": consent_id,
            "gcs_object_key": gcs_object_key,
            "content_type": content_type,
            "sha256": sha256,
            "capture_source": capture_source,
            "image_type": "garment",
            "created_at": _now(),
            "deleted_at": None,
            "deletion_state": None,
        }
        rows.append(rec)
        self._save("garment_images", rows)
        return rec

    def create_extraction_job(
        self,
        *,
        image_id: str,
        idempotency_key: str,
        status: str = "QUEUED",
    ) -> dict[str, Any]:
        rows = self._load("extraction_jobs")
        for r in rows:
            if r.get("idempotency_key") == idempotency_key:
                return r
        job_id = str(uuid.uuid4())
        rec = {
            "job_id": job_id,
            "id": job_id,
            "image_id": image_id,
            "status": status,
            "attempt_count": 0,
            "idempotency_key": idempotency_key,
            "model_version": None,
            "raw_result": None,
            "error_code": None,
            "started_at": None,
            "completed_at": None,
            "created_at": _now(),
        }
        rows.append(rec)
        self._save("extraction_jobs", rows)
        return rec

    def get_extraction_job(self, job_id: str) -> Optional[dict[str, Any]]:
        for r in self._load("extraction_jobs"):
            if r.get("job_id") == job_id or r.get("id") == job_id or r.get("idempotency_key") == job_id:
                return r
        return None

    def update_extraction_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        rows = self._load("extraction_jobs")
        for r in rows:
            if r.get("job_id") == job_id or r.get("id") == job_id:
                r.update(fields)
                self._save("extraction_jobs", rows)
                return r
        raise ValueError("job not found")

    def apply_attributes(
        self,
        garment_instance_id: str,
        attributes: dict[str, Any],
        *,
        actor_user_id: str,
        review_status: Optional[str] = None,
    ) -> dict[str, Any]:
        garments = self.list_garment_instances(include_deleted=True)
        target = None
        for g in garments:
            if g.get("garment_instance_id") == garment_instance_id or g.get("id") == garment_instance_id:
                target = g
                break
        if not target or target.get("deleted_at"):
            raise ValueError("garment not found")

        corrections = self._load("attribute_corrections")
        for field, new_val in attributes.items():
            if field in {"garment_instance_id", "id", "created_at"}:
                continue
            old_val = target.get(field)
            if old_val == new_val:
                continue
            corrections.append({
                "correction_id": str(uuid.uuid4()),
                "garment_instance_id": garment_instance_id,
                "field_name": field,
                "old_value": old_val,
                "new_value": new_val,
                "actor_user_id": actor_user_id,
                "created_at": _now(),
            })
            target[field] = new_val
        if review_status:
            target["review_status"] = review_status
        target["updated_at"] = _now()
        self._save("garment_instances", garments)
        self._save("attribute_corrections", corrections)
        self.audit("garment_attributes_patched", garment_instance_id=garment_instance_id, actor=actor_user_id)
        return target

    def brand_coverage_summary(self) -> dict[str, Any]:
        all_g = self.list_garment_instances()
        verified = self.list_verified_brand_observations()
        return {
            "garment_instances_total": len(all_g),
            "verified_branded_observations": len(verified),
            "needs_review": sum(1 for g in all_g if g.get("review_status") == "needs_review"),
            "brand_analytics_available": len(verified) > 0,
            "empty_state": (
                None
                if verified
                else "No verified consented garment brand observations yet. "
                     "Brand share and competitor benchmarks remain unavailable."
            ),
        }

    # ----- campaigns -----
    def create_campaign(self, *, tenant_id: str, image_object_key: str, metadata: dict | None = None) -> dict[str, Any]:
        rows = self._load("campaigns")
        cid = str(uuid.uuid4())
        rec = {
            "campaign_id": cid,
            "id": cid,
            "tenant_id": tenant_id,
            "image_object_key": image_object_key,
            "metadata": metadata or {},
            "created_at": _now(),
            "deleted_at": None,
        }
        rows.append(rec)
        self._save("campaigns", rows)
        return rec

    def get_campaign(self, campaign_id: str) -> Optional[dict[str, Any]]:
        for c in self._load("campaigns"):
            if c.get("campaign_id") == campaign_id or c.get("id") == campaign_id:
                return c
        return None

    def save_campaign_evaluations(self, campaign_id: str, evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [r for r in self._load("campaign_evaluations") if r.get("campaign_id") != campaign_id]
        out = []
        for ev in evaluations:
            rec = {
                "evaluation_id": str(uuid.uuid4()),
                "campaign_id": campaign_id,
                **ev,
                "created_at": _now(),
            }
            rows.append(rec)
            out.append(rec)
        self._save("campaign_evaluations", rows)
        return out

    def list_campaign_evaluations(self, campaign_id: str) -> list[dict[str, Any]]:
        return [r for r in self._load("campaign_evaluations") if r.get("campaign_id") == campaign_id]

    # ----- catalog / saved views -----
    def upsert_catalog_sku(self, sku: dict[str, Any]) -> dict[str, Any]:
        rows = self._load("brand_catalog_skus")
        for r in rows:
            if r.get("brand_id") == sku.get("brand_id") and r.get("brand_sku") == sku.get("brand_sku"):
                r.update(sku)
                r["updated_at"] = _now()
                self._save("brand_catalog_skus", rows)
                return r
        sku = {**sku, "sku_id": str(uuid.uuid4()), "created_at": _now(), "updated_at": _now()}
        rows.append(sku)
        self._save("brand_catalog_skus", rows)
        return sku

    def list_catalog(self, brand_id: Optional[str] = None) -> list[dict[str, Any]]:
        rows = self._load("brand_catalog_skus")
        if brand_id:
            return [r for r in rows if r.get("brand_id") == brand_id]
        return rows

    def save_view(self, *, tenant_id: str, name: str, page: str, filters: dict) -> dict[str, Any]:
        rows = self._load("saved_views")
        rec = {
            "view_id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "name": name,
            "page": page,
            "filters": filters,
            "created_at": _now(),
        }
        rows.append(rec)
        self._save("saved_views", rows)
        return rec

    def list_saved_views(self, tenant_id: str) -> list[dict[str, Any]]:
        return [r for r in self._load("saved_views") if r.get("tenant_id") == tenant_id]
