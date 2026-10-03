-- Phase 9, Admiral slice 2: questions through the mailbox, and replanning. Expand-only.
ALTER TABLE plans ADD COLUMN IF NOT EXISTS replans integer NOT NULL DEFAULT 0;           -- how many times it changed course
ALTER TABLE plans ADD COLUMN IF NOT EXISTS changes jsonb NOT NULL DEFAULT '[]'::jsonb;   -- each change: why, and the new steps
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS approved boolean NOT NULL DEFAULT false;   -- he OK'd it: it runs as he saw it
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS handled boolean NOT NULL DEFAULT false;    -- its failure was already replanned (or given up on)
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS superseded boolean NOT NULL DEFAULT false; -- a replan took another route instead
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS mailbox_id bigint;                         -- the card asking him (a question or an OK)
