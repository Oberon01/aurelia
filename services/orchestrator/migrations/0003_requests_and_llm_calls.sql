-- Phase 1: every request to Aurelia and every model call, for timing,
-- cost tracking (Libra) and pattern analysis (Parallax). Expand-only.
CREATE TABLE ask_requests (
    id          uuid PRIMARY KEY,
    session_id  text NOT NULL,
    user_id     text NOT NULL,
    text        text NOT NULL,
    route       text NOT NULL,              -- fastpath | llm | error
    reply       text,
    task_ids    uuid[] NOT NULL DEFAULT '{}',
    latency_ms  bigint,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE llm_calls (
    id                bigserial PRIMARY KEY,
    request_id        uuid,                         -- the ask_requests row it served
    provider          text NOT NULL,
    model             text NOT NULL,
    input_tokens      int NOT NULL DEFAULT 0,
    output_tokens     int NOT NULL DEFAULT 0,
    cache_read_tokens int NOT NULL DEFAULT 0,
    latency_ms        bigint,
    at                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ask_requests_created ON ask_requests (created_at DESC);
CREATE INDEX llm_calls_at ON llm_calls (at DESC);
