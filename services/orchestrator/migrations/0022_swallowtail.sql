-- Swallowtail: field checks of each release, run in his home once it's live (config/checks/*.yaml). Expand-only.
--
-- A run is one go at a set of checks files: a release's (once, right after it goes live) or ones he asked for.
-- Each check is a row: what it needs (auto: only looks; start: acts or needs him), its status, the input given,
-- what was expected, what actually happened, and one line per expectation.
CREATE TABLE IF NOT EXISTS check_runs (
    id           bigserial PRIMARY KEY,
    release_id   bigint,                              -- the release whose checks these are (none: he asked)
    title        text NOT NULL,
    files        text[] NOT NULL DEFAULT '{}',
    by_whom      text NOT NULL DEFAULT 'release',     -- release | you
    status       text NOT NULL DEFAULT 'running',     -- running | waiting (for his Start or answers) | done
    summary      jsonb,                               -- how many of each status
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS check_runs_release ON check_runs (release_id) WHERE release_id IS NOT NULL AND by_whom = 'release';
CREATE INDEX IF NOT EXISTS check_runs_recent ON check_runs (id DESC);

CREATE TABLE IF NOT EXISTS check_results (
    id           bigserial PRIMARY KEY,
    run_id       bigint NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
    file         text NOT NULL,
    check_id     text NOT NULL,
    name         text NOT NULL,
    ord          integer NOT NULL DEFAULT 0,
    spec         jsonb,                               -- the check as written (so a later edit doesn't change this run)
    needs        text NOT NULL DEFAULT 'auto',        -- auto | start
    status       text NOT NULL DEFAULT 'pending',     -- pending | running | passed | failed | needs_start | needs_you | skipped | error
    input        jsonb,                               -- what was said or called (his names filled in)
    expected     jsonb,
    actual       jsonb,                               -- what actually happened: route, reply, tools and results, confirmations
    lines        jsonb,                               -- each expectation: what, expected, actual, ok
    note         text,
    asking       text,                                -- info | do | ask: what the panel shows him for this check now
    question     text,
    answer       boolean,
    started_at   timestamptz,
    finished_at  timestamptz,
    ms           integer
);
CREATE INDEX IF NOT EXISTS check_results_run ON check_results (run_id, file, ord);
