-- Her understanding of her own releases. Chrysalis keeps each commit's whole message (subject and
-- body); she writes her notes on each release that goes live: what it did, why, what she can do now,
-- what it touches, and anything Kentrell has to do. Expand-only.
ALTER TABLE releases ADD COLUMN IF NOT EXISTS commits jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE releases ADD COLUMN IF NOT EXISTS notes jsonb;
