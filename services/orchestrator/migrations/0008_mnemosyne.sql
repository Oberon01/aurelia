-- Phase 6: Mnemosyne ("Mnemo"), the record. Expand-only.
--
-- Raw requests stay in ask_requests (written with every request). A background
-- loop turns quiet conversations into records: what was asked, decided and left
-- open, embedded for recall by meaning. A record covers one stretch of a session
-- between quiet gaps; `skipped` marks stretches with nothing worth summarizing
-- (a single fast-path command), so they aren't looked at again.
CREATE TABLE conversation_records (
    id             bigserial PRIMARY KEY,
    session_id     text NOT NULL,
    user_id        text NOT NULL,
    started_at     timestamptz NOT NULL,
    ended_at       timestamptz NOT NULL,          -- the last request covered; later ones belong to the next record
    request_ids    uuid[] NOT NULL DEFAULT '{}',
    requests       integer NOT NULL DEFAULT 0,
    skipped        boolean NOT NULL DEFAULT false,
    summary        text,
    decisions      jsonb NOT NULL DEFAULT '[]',
    open_items     jsonb NOT NULL DEFAULT '[]',
    topics         text[] NOT NULL DEFAULT '{}',
    model          text,                          -- which model wrote it; 'none' = a plain list when no model could
    source_text    text NOT NULL DEFAULT '',      -- what gets embedded, and searched when embeddings are off
    embedding      vector(768),
    embed_tried_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX conversation_records_session ON conversation_records (session_id, user_id, ended_at DESC);
CREATE INDEX conversation_records_user ON conversation_records (user_id, ended_at DESC) WHERE NOT skipped;

-- Things Kentrell asked Aurelia to remember. Only ever written when he asks;
-- shown in a panel where each one can be deleted.
CREATE TABLE remembered_facts (
    id             bigserial PRIMARY KEY,
    user_id        text NOT NULL,
    fact           text NOT NULL,
    source_request uuid,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX remembered_facts_user ON remembered_facts (user_id, created_at);
