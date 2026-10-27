-- suffai-similarity: track that each photo's BigQuery observation row was written.
-- Garment counts per PIN live in BigQuery (suffai_ops.garment_items). A failed insert is retried
-- (Pub/Sub) and resent from this table instead of being dropped; garment_pin_counts is no longer used.
-- Safe to re-run.

ALTER TABLE garment_similarity_events
  ADD COLUMN IF NOT EXISTS order_id TEXT,
  ADD COLUMN IF NOT EXISTS item_id TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'garment_similarity_events'
      AND column_name = 'bq_synced_at'
  ) THEN
    ALTER TABLE garment_similarity_events ADD COLUMN bq_synced_at TIMESTAMPTZ;
    -- Events from before this release count as sent; reconcile_data.py --apply requeues any whose
    -- photo has no BigQuery observation.
    UPDATE garment_similarity_events SET bq_synced_at = created_at;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_garment_similarity_events_bq_pending
  ON garment_similarity_events (created_at)
  WHERE bq_synced_at IS NULL;
