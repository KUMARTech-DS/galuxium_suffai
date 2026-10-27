-- One set of values for every tab and service. Safe to re-run.
--
--   ./migrate_bq_consistency.sh YOUR_PROJECT_ID [dataset]
--
-- 1) pin_areas: PIN area name, population and centroid, filled by Gemini + Google Search
--    (Geo "explore a PIN", Admin create customer, backfill_pin_areas.py).
-- 2) ref_values: canonical value per (kind, alias_key). alias_key = lower case, letters and digits only,
--    so "Levi's", "Levis" and "LEVI'S" share the key levis. suffai-extraction and the Command Center
--    map every value they write through this table.
-- 3) Existing customers / commodities values are rewritten to the canonical spelling.
-- 4) garment_items leaves out CSV rows superseded by photos of the same order.
-- Run migrate_bq_unified.sql first on datasets created before it.

CREATE TABLE IF NOT EXISTS `suffai_ops.pin_areas` (
  pin_code STRING NOT NULL,
  city STRING,
  area_label STRING,
  population_pin INT64,
  population_year INT64,
  area_sq_km FLOAT64,
  pop_buffer_1km INT64,
  pop_buffer_2km INT64,
  pop_buffer_4km INT64,
  centroid_lat FLOAT64,
  centroid_lon FLOAT64,
  status STRING,
  detail STRING,
  sources STRING,
  search_entry_html STRING,
  model STRING,
  fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `suffai_ops.ref_values` (
  kind STRING NOT NULL,
  canonical STRING NOT NULL,
  alias_key STRING NOT NULL
);

-- Fixed vocabularies and common synonyms. Existing keys are never overwritten.
MERGE `suffai_ops.ref_values` t
USING (
  SELECT * FROM UNNEST([
    STRUCT('customer_gender' AS kind, 'Female' AS canonical, 'female' AS alias_key),
    ('customer_gender', 'Female', 'f'), ('customer_gender', 'Female', 'woman'), ('customer_gender', 'Female', 'women'),
    ('customer_gender', 'Male', 'male'), ('customer_gender', 'Male', 'm'), ('customer_gender', 'Male', 'man'),
    ('customer_gender', 'Male', 'men'), ('customer_gender', 'Other', 'other'), ('customer_gender', 'Other', 'o'),
    ('garment_gender', 'Male', 'male'), ('garment_gender', 'Male', 'm'), ('garment_gender', 'Male', 'men'),
    ('garment_gender', 'Male', 'mens'), ('garment_gender', 'Female', 'female'), ('garment_gender', 'Female', 'f'),
    ('garment_gender', 'Female', 'women'), ('garment_gender', 'Female', 'womens'),
    ('garment_gender', 'Unisex', 'unisex'), ('garment_gender', 'Unisex', 'u'), ('garment_gender', 'Unisex', 'other'),
    ('garment_gender', 'Kids', 'kids'), ('garment_gender', 'Kids', 'kid'), ('garment_gender', 'Kids', 'boys'),
    ('garment_gender', 'Kids', 'girls'),
    ('color', 'Navy Blue', 'navy'), ('color', 'Olive Green', 'olive'), ('color', 'Grey', 'gray'),
    ('color', 'Off White', 'offwhite'),
    ('garment_name', 'T-Shirt', 'tshirt'), ('garment_name', 'T-Shirt', 'tee'), ('garment_name', 'T-Shirt', 'teeshirt'),
    ('garment_name', 'Hoodie', 'hoody'), ('garment_name', 'Trousers', 'trouser'), ('garment_name', 'Trousers', 'pants')
  ])
) s
ON t.kind = s.kind AND t.alias_key = s.alias_key
WHEN NOT MATCHED THEN INSERT (kind, canonical, alias_key) VALUES (s.kind, s.canonical, s.alias_key);

-- Brand logins spell their brand the canonical way, so they are added before the data spellings.
BEGIN
  MERGE `suffai_ops.ref_values` t
  USING (
    SELECT 'brand' AS kind, REGEXP_REPLACE(LOWER(TRIM(brand_name)), r'[^a-z0-9]', '') AS alias_key,
           ANY_VALUE(TRIM(brand_name)) AS canonical
    FROM `suffai_ops.login_cred_db`
    WHERE role = 'brand' AND brand_name IS NOT NULL AND TRIM(brand_name) != ''
    GROUP BY alias_key
  ) s
  ON t.kind = s.kind AND t.alias_key = s.alias_key
  WHEN NOT MATCHED THEN INSERT (kind, canonical, alias_key) VALUES (s.kind, s.canonical, s.alias_key);
EXCEPTION WHEN ERROR THEN
  SELECT CONCAT('login_cred_db not used for brand names: ', @@error.message) AS note;
END;

-- Every other spelling already in the data: the most frequent spelling of each key wins.
MERGE `suffai_ops.ref_values` t
USING (
  WITH vals AS (
    SELECT 'brand' AS kind, TRIM(brand) AS v FROM `suffai_ops.commodities`
    UNION ALL SELECT 'color', TRIM(color) FROM `suffai_ops.commodities`
    UNION ALL SELECT 'garment_name', TRIM(name) FROM `suffai_ops.commodities`
  ),
  counted AS (
    SELECT kind, v, REGEXP_REPLACE(LOWER(v), r'[^a-z0-9]', '') AS alias_key, COUNT(*) AS n
    FROM vals WHERE v IS NOT NULL AND v != ''
    GROUP BY kind, v, alias_key
  )
  SELECT kind, alias_key, ARRAY_AGG(v ORDER BY n DESC, v LIMIT 1)[OFFSET(0)] AS canonical
  FROM counted
  WHERE alias_key NOT IN ('', 'unknown', 'none', 'null', 'na', 'notapplicable')
  GROUP BY kind, alias_key
) s
ON t.kind = s.kind AND t.alias_key = s.alias_key
WHEN NOT MATCHED THEN INSERT (kind, canonical, alias_key) VALUES (s.kind, s.canonical, s.alias_key);

-- Rewrite existing values to the canonical spelling.
UPDATE `suffai_ops.customers` c
SET gender = r.canonical
FROM `suffai_ops.ref_values` r
WHERE r.kind = 'customer_gender'
  AND r.alias_key = REGEXP_REPLACE(LOWER(TRIM(c.gender)), r'[^a-z0-9]', '')
  AND c.gender != r.canonical;

UPDATE `suffai_ops.commodities` c
SET brand = r.canonical
FROM `suffai_ops.ref_values` r
WHERE r.kind = 'brand'
  AND r.alias_key = REGEXP_REPLACE(LOWER(TRIM(c.brand)), r'[^a-z0-9]', '')
  AND c.brand != r.canonical;

UPDATE `suffai_ops.commodities` c
SET color = r.canonical
FROM `suffai_ops.ref_values` r
WHERE r.kind = 'color'
  AND r.alias_key = REGEXP_REPLACE(LOWER(TRIM(c.color)), r'[^a-z0-9]', '')
  AND c.color != r.canonical;

UPDATE `suffai_ops.commodities` c
SET name = r.canonical
FROM `suffai_ops.ref_values` r
WHERE r.kind = 'garment_name'
  AND r.alias_key = REGEXP_REPLACE(LOWER(TRIM(c.name)), r'[^a-z0-9]', '')
  AND c.name != r.canonical;

UPDATE `suffai_ops.commodities` c
SET gender = r.canonical
FROM `suffai_ops.ref_values` r
WHERE r.kind = 'garment_gender'
  AND r.alias_key = REGEXP_REPLACE(LOWER(TRIM(c.gender)), r'[^a-z0-9]', '')
  AND c.gender != r.canonical;

-- Garment items with their similarity outcome. is_repeat = the same customer's garment photographed again,
-- so dashboards count each real garment once. CSV rows of an order are superseded once its first photo
-- arrives (suffai-extraction), so an order never counts both.
CREATE OR REPLACE VIEW `suffai_ops.garment_items` AS
WITH latest_obs AS (
  SELECT order_id, item_id, garment_id, outcome, similarity_score
  FROM `suffai_ops.garment_similarity_observations`
  WHERE order_id IS NOT NULL AND item_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id, item_id ORDER BY event_ts DESC) = 1
)
SELECT
  c.*,
  o.garment_id AS unique_garment_id,
  o.outcome AS similarity_outcome,
  o.similarity_score AS similarity_score,
  IFNULL(o.outcome IN ('user_match', 'user_sku_match'), FALSE) AS is_repeat
FROM `suffai_ops.commodities` AS c
LEFT JOIN latest_obs AS o
  ON o.order_id = c.order_id AND o.item_id = c.item_id
WHERE IFNULL(c.image_status, '') != 'superseded';
