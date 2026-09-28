-- Every task Aurelia runs, even instant ones. Metadata is kept long-term for
-- timing, recurrence and criticality analysis; result payloads expire by tier.
CREATE TABLE task_runs (
    id            uuid PRIMARY KEY,
    tool          text NOT NULL,
    args          jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'running',   -- running | completed | cancelled | failed
    trigger       text NOT NULL DEFAULT 'user',      -- user | automation | sentinel
    progress_unit text,
    progress_current double precision,
    progress_total   double precision,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    duration_ms   bigint,
    partial       boolean NOT NULL DEFAULT false,
    result        jsonb,                              -- hot-tier payload; summarised/cleared by retention jobs
    error         text
);

CREATE INDEX task_runs_tool_started ON task_runs (tool, started_at DESC);
CREATE INDEX task_runs_status ON task_runs (status) WHERE status = 'running';
