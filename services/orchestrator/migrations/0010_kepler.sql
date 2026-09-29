-- Phase 6: Kepler, research and learning. Expand-only.
--
-- Every piece of research is kept with its question, answer and sources, and
-- belongs to a topic found by meaning. A topic holds the combined understanding
-- across its research: what's new since last time and where sources disagree.
CREATE TABLE kepler_topics (
    id               bigserial PRIMARY KEY,
    user_id          text NOT NULL DEFAULT 'owner',
    name             text NOT NULL,
    summary          text NOT NULL DEFAULT '',
    whats_new        text NOT NULL DEFAULT '',
    disagreements    jsonb NOT NULL DEFAULT '[]',
    research_count   integer NOT NULL DEFAULT 0,
    trilium_note_id  text,                         -- one-way copy in his Trilium
    embedding        vector(768),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kepler_research (
    id            bigserial PRIMARY KEY,
    user_id       text NOT NULL DEFAULT 'owner',
    topic_id      bigint REFERENCES kepler_topics (id) ON DELETE SET NULL,
    question      text NOT NULL,
    headline      text NOT NULL DEFAULT '',
    answer        text NOT NULL DEFAULT '',
    key_points    jsonb NOT NULL DEFAULT '[]',
    disagreements jsonb NOT NULL DEFAULT '[]',
    sources       jsonb NOT NULL DEFAULT '[]',   -- [{n, title, url, site}]
    depth         text NOT NULL DEFAULT 'quick',   -- quick (3 sources) | thorough (8)
    task_id       uuid,
    source_text   text NOT NULL DEFAULT '',        -- what gets embedded, and searched without embeddings
    embedding     vector(768),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX kepler_research_user ON kepler_research (user_id, created_at DESC);
CREATE INDEX kepler_research_topic ON kepler_research (topic_id, created_at DESC);
CREATE INDEX kepler_topics_user ON kepler_topics (user_id, updated_at DESC);

-- "Quiz me": how he did, so what he's learned can be proposed to his profile.
CREATE TABLE quiz_results (
    id          bigserial PRIMARY KEY,
    user_id     text NOT NULL DEFAULT 'owner',
    subject     text NOT NULL,
    topic_id    bigint,
    score       integer NOT NULL,
    total       integer NOT NULL,
    missed      jsonb NOT NULL DEFAULT '[]',     -- the questions he got wrong
    created_at  timestamptz NOT NULL DEFAULT now()
);
