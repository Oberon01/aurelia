-- Phase 9, Admiral's scheduling engine: timers that survive restarts. Expand-only.
--
-- A job is what to run (a tool call, a plan, a reminder, or one of her own system jobs), when (once, every N
-- minutes, or on days at a time, in his time zone) and its rules (grace for a missed run, a heads-up first).
-- One runner claims due jobs with a lease (FOR UPDATE SKIP LOCKED), so two orchestrators during a deploy
-- never both run one, and a job interrupted by a restart is picked up again.
CREATE TABLE IF NOT EXISTS jobs (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,                       -- tool | plan | reminder | system
    name          text,                                -- a system job's handler (unique while scheduled)
    title         text NOT NULL,                       -- what he sees: "Lock the front door"
    user_id       text NOT NULL DEFAULT 'owner',
    created_by    text NOT NULL DEFAULT 'owner',       -- owner (he asked) | system
    spec          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- tool+args | goal | text | a system job's state
    rule          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"once": true} | {"every_minutes": n} | {"days": [...], "time": "HH:MM"}
    tz            text NOT NULL DEFAULT 'America/Chicago',
    planned_at    timestamptz,                         -- when he was told it would run (grace is measured from it)
    due_at        timestamptz NOT NULL,                -- when this occurrence runs (a heads-up may move it a little)
    next_at       timestamptz NOT NULL,                -- when the runner next looks (the heads-up, or due_at)
    phase         text NOT NULL DEFAULT 'run',         -- heads_up | run
    heads_up      boolean NOT NULL DEFAULT false,
    grace_seconds integer,                             -- how late it may still run; NULL = always (system jobs)
    status        text NOT NULL DEFAULT 'scheduled',   -- scheduled | running | waiting | done | cancelled | missed | failed
    device        integer,                             -- the display that asked
    task_id       uuid,                                -- this occurrence's task, once started (resumed, never re-run)
    mailbox_id    bigint,                              -- a change-tier job's "OK?" card
    tries         integer NOT NULL DEFAULT 0,
    runs          integer NOT NULL DEFAULT 0,
    last_outcome  text,
    last_run_at   timestamptz,
    lease_until   timestamptz,
    claimed_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS planned_at timestamptz;   -- (for a database that made jobs before it)
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, next_at);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_system_name ON jobs (name)
    WHERE created_by = 'system' AND status IN ('scheduled', 'running') AND (rule ? 'every_minutes');

CREATE TABLE IF NOT EXISTS job_runs (
    id            bigserial PRIMARY KEY,
    job_id        bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    due_at        timestamptz NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    late_seconds  integer NOT NULL DEFAULT 0,
    outcome       text NOT NULL,                       -- done | unconfirmed | failed | missed | waiting | cancelled | skipped
    task_id       uuid,
    note          text
);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs (job_id, started_at DESC);
CREATE INDEX IF NOT EXISTS job_runs_recent ON job_runs (started_at DESC);

-- Plans that wait for him: reminded after a while, expired after longer (the engine's sweep).
ALTER TABLE plans ADD COLUMN IF NOT EXISTS waiting_since timestamptz;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS reminded boolean NOT NULL DEFAULT false;
-- A failed step's retry waits a few minutes (a computer coming back online), on the engine.
ALTER TABLE plan_steps ADD COLUMN IF NOT EXISTS not_before timestamptz;
