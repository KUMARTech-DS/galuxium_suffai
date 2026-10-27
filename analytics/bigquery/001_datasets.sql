-- BigQuery datasets (suffai-dev / staging / prod projects)
-- Run after project/region confirmation. Do not deploy without billing approval.

CREATE SCHEMA IF NOT EXISTS suffai_raw OPTIONS(description="Immutable source snapshots");
CREATE SCHEMA IF NOT EXISTS suffai_staging OPTIONS(description="Validated / unnested staging");
CREATE SCHEMA IF NOT EXISTS suffai_marts OPTIONS(description="Privacy-safe aggregates");
