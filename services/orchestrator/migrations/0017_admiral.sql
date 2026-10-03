-- Phase 9, Admiral: a big request becomes a checked plan of governed steps. The plan and every step
-- are kept here, so a plan survives a restart and picks up where it was. Expand-only.
CREATE TABLE IF NOT EXISTS plans (
    id          uuid PRIMARY KEY,
    user_id     text NOT NULL DEFAULT 'owner',
    goal        text NOT NULL,
    device      integer,                              -- the display that asked (its panel, its notification)
    task_id     uuid,                                 -- the admiral.plan task whose panel shows the plan
    status      text NOT NULL DEFAULT 'planning',     -- planning | running | waiting | done | gaps | failed | stopped
    report      jsonb,                                -- the final check: headline, summary, meets_goal, gaps
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plans_status ON plans (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS plan_steps (
    plan_id     uuid NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    n           integer NOT NULL,
    title       text NOT NULL,
    tool        text NOT NULL,
    args        jsonb NOT NULL DEFAULT '{}'::jsonb,
    depends     integer[] NOT NULL DEFAULT '{}',
    fill        boolean NOT NULL DEFAULT false,       -- its arguments come from earlier steps' results
    status      text NOT NULL DEFAULT 'pending',      -- pending | running | waiting | done | unconfirmed | failed | skipped
    task_id     uuid,
    result      jsonb,
    note        text,                                 -- why it waits, failed, or isn't confirmed
    attempts    integer NOT NULL DEFAULT 0,
    started_at  timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (plan_id, n)
);
