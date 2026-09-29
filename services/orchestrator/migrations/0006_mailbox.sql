-- Phase 4: the review mailbox, and what approvals change. Expand-only.
--
-- Aurelia infers on her own, but nothing she infers changes her behaviour until
-- Kentrell approves it here. Every item has one shape: kind, proposal, evidence,
-- confidence, and what changes on approval.

CREATE TABLE mailbox_items (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,                  -- automation | criticality | retention (later: dependency, patch)
    fingerprint   text NOT NULL,                  -- the same idea → the same fingerprint (never re-proposed while decided)
    source        text NOT NULL DEFAULT 'parallax',
    title         text NOT NULL,
    summary       text NOT NULL DEFAULT '',
    proposal      jsonb NOT NULL DEFAULT '{}',    -- exactly what approval would set
    evidence      jsonb NOT NULL DEFAULT '[]',    -- lines of evidence, plain words
    strength      double precision NOT NULL DEFAULT 0,   -- how much evidence (e.g. days seen); a rejected idea returns only if this grows a lot
    confidence    double precision NOT NULL DEFAULT 0,   -- 0..1
    effect        text NOT NULL DEFAULT '',       -- what approval does, in plain words
    status        text NOT NULL DEFAULT 'waiting',-- waiting | approved | modified | rejected | deferred | expired
    urgent        boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    decided_at    timestamptz,
    decided_by    text,
    reason        text,                           -- why rejected (kept, so it isn't re-proposed)
    defer_until   timestamptz,
    applied       jsonb                           -- what was actually set (the proposal, or your modification)
);

CREATE INDEX mailbox_waiting ON mailbox_items (status, created_at DESC);
CREATE INDEX mailbox_fingerprint ON mailbox_items (fingerprint, created_at DESC);

-- Criticality per tool: louder failures, longer retention. Set only by approval.
CREATE TABLE tool_criticality (
    tool          text PRIMARY KEY,
    level         text NOT NULL,                  -- normal | high | critical
    reason        text,
    from_item     bigint REFERENCES mailbox_items (id),
    set_at        timestamptz NOT NULL DEFAULT now()
);

-- Longer full-panel retention per tool. Set only by approval.
CREATE TABLE tool_retention (
    tool          text PRIMARY KEY,
    hot_days      integer NOT NULL,
    from_item     bigint REFERENCES mailbox_items (id),
    set_at        timestamptz NOT NULL DEFAULT now()
);

-- Approved automations. Stored now; Pulsar (Phase 10) runs them.
CREATE TABLE automations (
    id            bigserial PRIMARY KEY,
    tool          text NOT NULL,
    args          jsonb NOT NULL DEFAULT '{}',
    schedule      jsonb NOT NULL,                 -- {"days": ["mon",…], "time": "07:30", "timezone": "America/Chicago"}
    status        text NOT NULL DEFAULT 'waiting_for_pulsar',   -- waiting_for_pulsar | active | paused
    from_item     bigint REFERENCES mailbox_items (id),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- One morning digest per day, so it shows once.
CREATE TABLE digests (
    day           date PRIMARY KEY,
    shown_at      timestamptz NOT NULL DEFAULT now(),
    content       jsonb NOT NULL
);
