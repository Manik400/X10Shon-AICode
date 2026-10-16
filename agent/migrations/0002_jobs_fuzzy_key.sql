-- 0002: persisted secondary (company|title|location) key for fuzzy duplicate detection.
ALTER TABLE jobs ADD COLUMN fuzzy_key TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_fuzzy_key ON jobs(fuzzy_key);
