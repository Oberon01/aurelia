-- Phase 3: memory. Expand-only.
--
-- task_runs stays the permanent metadata (tool, args, timing, outcome).
-- panel_snapshots holds what's needed to redraw a result panel, and expires by tier:
--   hot   (7 days)  full panel data               → restores the original panel
--   warm  (~90 days) compressed summary            → restores a lighter panel
--   cold  (beyond)   row deleted; metadata only    → nothing to restore
--   pinned           kept in full regardless of age
-- Days come from retention in config/aurelia.yaml.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE panel_snapshots (
    task_id       uuid PRIMARY KEY REFERENCES task_runs (id) ON DELETE CASCADE,
    tool          text NOT NULL,
    args          jsonb NOT NULL DEFAULT '{}',
    args_key      text NOT NULL,                  -- normalised args: "the same task" = same tool + args_key
    user_id       text NOT NULL DEFAULT 'owner',
    captured_at   timestamptz NOT NULL DEFAULT now(),
    partial       boolean NOT NULL DEFAULT false,
    tier          text NOT NULL DEFAULT 'hot',    -- hot | warm
    pinned        boolean NOT NULL DEFAULT false,
    panel         jsonb,                          -- hot: the result data exactly as the panel showed it
    summary       jsonb,                          -- warm: counts, top items
    source_text   text NOT NULL DEFAULT '',       -- what gets embedded, and searched when embeddings are off
    embedding     vector(768),                    -- recall by meaning (nomic-embed-text); NULL until embedded
    embed_tried_at timestamptz
);

CREATE INDEX panel_snapshots_latest ON panel_snapshots (user_id, tool, args_key, captured_at DESC);
CREATE INDEX panel_snapshots_tier_age ON panel_snapshots (tier, captured_at) WHERE NOT pinned;
CREATE INDEX panel_snapshots_unembedded ON panel_snapshots (captured_at) WHERE embedding IS NULL;

-- Daily per-tool stats, rolled up from task_runs so analysis stays fast.
CREATE TABLE tool_stats_daily (
    day           date NOT NULL,
    tool          text NOT NULL,
    runs          integer NOT NULL,
    completed     integer NOT NULL,
    failed        integer NOT NULL,
    cancelled     integer NOT NULL,
    median_ms     bigint,
    p90_ms        bigint,
    worst_ms      bigint,
    rolled_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, tool)
);

CREATE VIEW tool_stats_weekly AS
SELECT date_trunc('week', day)::date AS week, tool,
       sum(runs) AS runs, sum(completed) AS completed, sum(failed) AS failed, sum(cancelled) AS cancelled,
       max(worst_ms) AS worst_ms,
       (sum(median_ms * runs) / NULLIF(sum(runs), 0))::bigint AS typical_ms   -- run-weighted daily medians
FROM tool_stats_daily
GROUP BY 1, 2;
