-- Expand-only: adds who asked and at what tier. Old code keeps working
-- (defaults fill the new columns), so this is safe during a rolling deploy.
ALTER TABLE task_runs ADD COLUMN user_id text NOT NULL DEFAULT 'owner';
ALTER TABLE task_runs ADD COLUMN tier text;
ALTER TABLE task_runs ADD COLUMN confirmed boolean NOT NULL DEFAULT false;

-- Refused requests are logged too: what was asked, by whom, and why it was refused.
CREATE TABLE governance_denials (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    user_id    text NOT NULL,
    tool       text NOT NULL,
    tier       text,
    reason     text NOT NULL,
    args       jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX task_runs_user_started ON task_runs (user_id, started_at DESC);
