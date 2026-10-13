-- Efficiency (October 2026): indexes for the queries that scanned whole tables, and a nudge to the engine
-- when a job becomes due, so it can sleep until then instead of looking every 2 seconds.

-- task_runs: the hourly payload expiry and rollups, and the history prune, by when they started.
CREATE INDEX IF NOT EXISTS task_runs_started ON task_runs (started_at);
CREATE INDEX IF NOT EXISTS task_runs_with_result ON task_runs (started_at) WHERE result IS NOT NULL;
-- recent failures (the glance strip, every minute per display)
CREATE INDEX IF NOT EXISTS task_runs_failed ON task_runs (finished_at DESC) WHERE status = 'failed';

-- ask_requests: one conversation's turns (Mnemosyne, the suggestions strip).
CREATE INDEX IF NOT EXISTS ask_requests_session ON ask_requests (session_id, user_id, created_at);

-- governance_denials: pruned by age.
CREATE INDEX IF NOT EXISTS governance_denials_at ON governance_denials (at);

-- mailbox_items: deferred items come back by date.
CREATE INDEX IF NOT EXISTS mailbox_deferred ON mailbox_items (defer_until) WHERE status = 'deferred';

-- jobs: tell a listening engine that something became due (another orchestrator, a migration, a script).
CREATE OR REPLACE FUNCTION aurelia_jobs_due() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.status = 'scheduled' THEN
        PERFORM pg_notify('aurelia_jobs', '');
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jobs_due_notify ON jobs;
CREATE TRIGGER jobs_due_notify AFTER INSERT OR UPDATE OF status, next_at ON jobs
    FOR EACH ROW EXECUTE FUNCTION aurelia_jobs_due();

-- llm_calls: what was written to the prompt cache (billed as input, a little above it) counts toward the budget.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS cache_write_tokens bigint NOT NULL DEFAULT 0;
