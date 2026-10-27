"""
Cloud Run Job: incremental source ingest (Mongo CDC-style or JSON snapshot refresh).

- If MONGO_URI set: pull updated docs since watermark (updated_at overlap window).
- Else: re-read DATA_DIR / optional GCS raw bucket and bump watermark, then call bq_sync.

  python -m jobs.cdc_ingest
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from jobs.bq_sync import run as bq_sync_run
from suffai.ids import extract_datetime, extract_id

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("suffai.cdc")


def _load_watermark() -> datetime:
    path = settings.CDC_WATERMARK_PATH
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return datetime.fromisoformat(data["watermark"])
        except Exception:
            pass
    return datetime.now(timezone.utc) - timedelta(days=3650)


def _save_watermark(ts: datetime, stats: dict) -> None:
    path = settings.CDC_WATERMARK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"watermark": ts.isoformat(), "stats": stats}, indent=2) + "\n",
        encoding="utf-8",
    )


def _sync_mongo(since: datetime) -> dict:
    from pymongo import MongoClient

    client = MongoClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB]
    overlap = since - timedelta(hours=2)
    out_dir = settings.DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {}
    for coll in ("orders", "customers", "stores", "commodities", "services"):
        if coll not in db.list_collection_names():
            stats[coll] = 0
            continue
        query = {"updated_at": {"$gte": overlap}}
        docs = list(db[coll].find(query))
        # Merge into existing snapshot file (idempotent by _id)
        path = out_dir / f"{coll}.json"
        existing = []
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
        by_id = {extract_id(d.get("_id")): d for d in existing if extract_id(d.get("_id"))}
        for d in docs:
            # bson → json-friendly
            d = json.loads(json.dumps(d, default=str))
            by_id[extract_id(d.get("_id"))] = d
        merged = list(by_id.values())
        path.write_text(json.dumps(merged, indent=2, default=str) + "\n", encoding="utf-8")
        stats[coll] = len(docs)
        log.info("mongo %s pulled=%s total=%s", coll, len(docs), len(merged))
    return stats


def run() -> dict:
    since = _load_watermark()
    now = datetime.now(timezone.utc)
    stats: dict = {"mode": "json_snapshot", "since": since.isoformat()}

    if settings.MONGO_URI:
        stats["mode"] = "mongo_incremental"
        stats["mongo"] = _sync_mongo(since)
    else:
        log.info("MONGO_URI unset — refreshing warehouse from DATA_DIR=%s", settings.DATA_DIR)

    # Always push current curated snapshot into BigQuery when credentials available
    try:
        bq = bq_sync_run()
        stats["bigquery"] = bq
    except Exception as exc:
        log.warning("bq_sync skipped: %s", exc)
        stats["bigquery_error"] = str(exc)

    _save_watermark(now, stats)
    return stats


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
