-- Chrysalis: one row per release it evaluates. Expand-only.
CREATE TABLE releases (
    id            bigserial PRIMARY KEY,
    sha           text NOT NULL,
    prev_sha      text,
    author        text,
    message       text,
    changed_paths text[] NOT NULL DEFAULT '{}',
    services      text[] NOT NULL DEFAULT '{}',   -- services rebuilt and swapped
    plan          jsonb NOT NULL DEFAULT '{}',     -- what Chrysalis decided to do, and why
    status        text NOT NULL,                   -- planned | testing | deploying | verifying | live | failed | rolled_back | held | dry_run
    detail        text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE INDEX releases_started ON releases (started_at DESC);
CREATE INDEX releases_sha ON releases (sha);
