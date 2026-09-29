-- Phase 6: what Aurelia knows about Kentrell, in layers. Expand-only.
--
-- Core facts go with every request; the rest are recalled by meaning when they
-- fit what he's asking. Facts come from him ("remember that…") or from what she
-- noticed in a conversation and he approved in the mailbox ("learned").
ALTER TABLE remembered_facts
    ADD COLUMN IF NOT EXISTS category       text NOT NULL DEFAULT 'general',
    ADD COLUMN IF NOT EXISTS core           boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS source         text NOT NULL DEFAULT 'told',     -- told | learned
    ADD COLUMN IF NOT EXISTS from_item      bigint,                           -- the mailbox item that approved it
    ADD COLUMN IF NOT EXISTS from_record    bigint,                           -- the conversation it came from
    ADD COLUMN IF NOT EXISTS updated_at     timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS embedding      vector(768),
    ADD COLUMN IF NOT EXISTS embed_tried_at timestamptz;

CREATE INDEX IF NOT EXISTS remembered_facts_core ON remembered_facts (user_id) WHERE core;
CREATE INDEX IF NOT EXISTS remembered_facts_unembedded ON remembered_facts (created_at) WHERE embedding IS NULL;

-- How many things she noticed in each conversation (proposed to the mailbox).
ALTER TABLE conversation_records ADD COLUMN IF NOT EXISTS observations integer NOT NULL DEFAULT 0;
