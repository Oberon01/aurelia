-- Phase 6: conversation context. Expand-only.
-- What was on screen (panel numbers, the one in focus) when each request was made,
-- so a request like "download the third one" can be understood when read back later.
ALTER TABLE ask_requests ADD COLUMN IF NOT EXISTS screen jsonb;
