"""Export aggregate 1P campaign-fee features to BigQuery for offline model training.

Run this as a Cloud Run Job after the online audit path is operating. It intentionally
excludes customer IDs, garment attributes, message text, and any other PII.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def main() -> int:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("BQ_PROJECT") or "").strip()
    dataset = (os.getenv("BQ_DATASET") or "suffai_ops").strip()
    if not dsn or not project:
        print("DATABASE_URL and GOOGLE_CLOUD_PROJECT (or BQ_PROJECT) are required", file=sys.stderr)
        return 2

    import psycopg2  # type: ignore
    from google.cloud import bigquery  # type: ignore

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.campaign_id::text, c.est_audience,
                   (SELECT COUNT(*) FROM brand_skus s WHERE s.campaign_id = c.campaign_id),
                   cardinality(COALESCE(c.pin_codes, ARRAY[]::text[])),
                   COALESCE((SELECT SUM(j.comparisons) FROM campaign_jobs j WHERE j.campaign_id = c.campaign_id), 0),
                   COALESCE((SELECT SUM(EXTRACT(EPOCH FROM (j.finished_at - j.started_at)))
                             FROM campaign_jobs j WHERE j.campaign_id = c.campaign_id), 0),
                   c.channels, c.objective, c.duration_days, c.budget_inr,
                   c.platform_fee_recommended, c.platform_fee_final, c.fee_model_version,
                   c.fee_accepted_at IS NOT NULL
            FROM brand_campaigns c
            WHERE c.run_mode = '1p'
            """
        )
        rows = cur.fetchall()
    now = datetime.now(timezone.utc).isoformat()
    payload = [{
        "campaign_id": r[0], "as_of_at": now, "audience_count": r[1], "sku_count": r[2],
        "pin_count": r[3], "comparisons": r[4], "process_seconds": r[5],
        "channels": ",".join(r[6] or []), "objective": r[7], "duration_days": r[8],
        "media_budget_inr": float(r[9]) if r[9] is not None else None,
        "recommended_fee_inr": float(r[10]) if r[10] is not None else None,
        "final_fee_inr": float(r[11]) if r[11] is not None else None,
        "fee_accepted": bool(r[13]), "model_version": r[12],
    } for r in rows]
    errors = bigquery.Client(project=project).insert_rows_json(
        f"{project}.{dataset}.campaign_fee_features", payload
    ) if payload else []
    if errors:
        print(f"BigQuery insert errors: {errors}", file=sys.stderr)
        return 1
    print(f"Exported {len(payload)} aggregate campaign fee feature rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
