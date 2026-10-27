-- Idempotent rename if an older deploy created user_unique_garments.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'user_unique_garments'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'user_wardrobe'
  ) THEN
    ALTER TABLE user_unique_garments RENAME TO user_wardrobe;
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = 'idx_user_unique_garments_user' AND n.nspname = 'public'
  ) THEN
    ALTER INDEX idx_user_unique_garments_user RENAME TO idx_user_wardrobe_user;
  END IF;
END $$;
