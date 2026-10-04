-- Phase 10, Pulsar: his automations on Admiral's engine, routines, and what's worth interrupting him for.
-- Expand-only.
--
-- An automation is one of his jobs that repeats (its rule isn't "once"): by the clock (days and a time, every N
-- minutes, a day of the month) or on an event (a computer coming online, a display opening). An event's
-- automation waits as 'armed' until its event happens. 'paused' stops it until he resumes it (his word, or
-- too many failures in a row).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fails_in_row integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS jobs_armed ON jobs ((rule->>'on'), (rule->>'device')) WHERE status = 'armed';

-- Automations he approved from Parallax's suggestions before Pulsar existed: each now becomes a job.
ALTER TABLE automations ADD COLUMN IF NOT EXISTS job_id bigint;

-- What I noticed that might be worth telling him: a door unlocked at night, a server running out of disk, an
-- update that failed. Each kind's severity is his (governance: pulsar.yaml); the severity decides how he hears.
-- One open notice per kind and key; it's cleared when the thing it's about goes away.
CREATE TABLE IF NOT EXISTS notices (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,                       -- door_unlocked_at_night | server_disk_low | computer_down | ...
    key         text NOT NULL,                       -- which one ("Front Door", "node:3", "release:41")
    severity    text NOT NULL,                       -- critical | high | normal | low (when it was raised)
    title       text NOT NULL,
    text        text NOT NULL DEFAULT '',
    status      text NOT NULL DEFAULT 'open',        -- open | cleared
    seen        boolean NOT NULL DEFAULT false,      -- he said "got it": no more repeats until it clears
    held        boolean NOT NULL DEFAULT false,      -- his phone hears when quiet hours end
    count       integer NOT NULL DEFAULT 1,
    mailbox_id  bigint,
    first_at    timestamptz NOT NULL DEFAULT now(),
    last_at     timestamptz NOT NULL DEFAULT now(),
    told_at     timestamptz,
    cleared_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS notices_open ON notices (kind, key) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS notices_recent ON notices (last_at DESC);
