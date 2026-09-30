-- Phase 7: every device that talks to Aurelia from outside the stack (a browser, the
-- phone, a Constellation node, a terminal) has its own key, approved through the
-- mailbox or on the server. Only a hash of each key is stored. Expand-only.
CREATE TABLE IF NOT EXISTS devices (
    id              serial PRIMARY KEY,
    kind            text NOT NULL,                      -- browser | node | terminal
    name            text NOT NULL,
    user_id         text NOT NULL DEFAULT 'owner',
    status          text NOT NULL DEFAULT 'pending',    -- pending | approved | declined | expired | revoked
    pair_code       text,                               -- shown on the device and on its mailbox card
    claim_hash      text,                               -- the requester's secret, to collect its key once
    key_hash        text UNIQUE,
    key_issued_at   timestamptz,
    info            jsonb NOT NULL DEFAULT '{}'::jsonb, -- os, version, hostname, browser…
    mailbox_item    integer,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    approved_at     timestamptz,
    revoked_at      timestamptz,
    last_seen       timestamptz
);
CREATE INDEX IF NOT EXISTS devices_pending ON devices (status, requested_at);
