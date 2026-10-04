-- Phase 9, Morpho: synthesis. Plans that worked become skills (versioned, approved by him), skills learn
-- their variations, tools he uses together are proposed as skills, and a low-impact action he keeps
-- approving can earn trust (exactly scoped, from an allow-list in his governance). Expand-only.
CREATE TABLE IF NOT EXISTS skills (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,                        -- what he calls it: "Best computer under a budget"
    version      integer NOT NULL DEFAULT 1,
    status       text NOT NULL DEFAULT 'proposed',     -- proposed | active | superseded | retired | declined
    slots        jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{"name": "budget", "example": "$1000"}]
    steps        jsonb NOT NULL,                       -- plan steps with {slot} placeholders; a step may offer options
    report       text NOT NULL DEFAULT '',             -- what the final report covers ('' = no model check)
    phrases      jsonb NOT NULL DEFAULT '[]'::jsonb,   -- ["find the best {product} under {budget}"]: its fast paths
    source       text NOT NULL DEFAULT 'plan',         -- plan | together (tools he used together) | variation
    from_plan    uuid,
    parent_id    bigint REFERENCES skills(id),         -- the version this one changes
    note         text,                                 -- why it was proposed (what changed, what he did)
    mailbox_id   bigint,
    uses         integer NOT NULL DEFAULT 0,
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    approved_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS skills_active_name ON skills (lower(name)) WHERE status = 'active';

ALTER TABLE plans ADD COLUMN IF NOT EXISTS skill_id bigint REFERENCES skills(id);
ALTER TABLE plans ADD COLUMN IF NOT EXISTS slots jsonb;

-- Every OK or no he gives to an action that asked (any card, the panel, the reply): what trust is earned from.
CREATE TABLE IF NOT EXISTS confirmations (
    id        bigserial PRIMARY KEY,
    tool      text NOT NULL,
    args_key  text NOT NULL,
    args      jsonb NOT NULL DEFAULT '{}'::jsonb,
    user_id   text NOT NULL DEFAULT 'owner',
    approved  boolean NOT NULL,
    via       text NOT NULL,                           -- reply | plan | schedule
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS confirmations_key ON confirmations (tool, args_key, at);

CREATE TABLE IF NOT EXISTS trust_grants (
    id           bigserial PRIMARY KEY,
    tool         text NOT NULL,
    args_key     text NOT NULL,                        -- exactly this action: the tool with these arguments
    args         jsonb NOT NULL DEFAULT '{}'::jsonb,
    title        text NOT NULL,
    user_id      text NOT NULL DEFAULT 'owner',
    status       text NOT NULL DEFAULT 'proposed',     -- proposed | active | revoked | declined
    mailbox_id   bigint,
    approvals    integer NOT NULL DEFAULT 0,           -- what earned it
    uses         integer NOT NULL DEFAULT 0,
    last_used_at timestamptz,
    granted_at   timestamptz,
    revoked_at   timestamptz,
    why_revoked  text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS trust_live ON trust_grants (tool, args_key) WHERE status IN ('proposed', 'active');

ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS trusted_grant bigint;   -- ran without asking, under this grant
