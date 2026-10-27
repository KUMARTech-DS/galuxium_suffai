-- Visual fingerprints of warehouse catalog photos (one per style × colour, so size never matters).
-- Filled lazily by suffai-extraction the first time a customer photo of that brand arrives.
-- embedded_object_key is the photo the fingerprint was made from; a replaced photo is re-embedded.
ALTER TABLE retail_product_images ADD COLUMN IF NOT EXISTS embedding JSONB;
ALTER TABLE retail_product_images ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE retail_product_images ADD COLUMN IF NOT EXISTS embedding_dim INT;
ALTER TABLE retail_product_images ADD COLUMN IF NOT EXISTS embedded_object_key TEXT;
