-- Beacon: your email (Gmail) and calendars (Phase 12). Expand-only.
--
-- Secrets (your Gmail app password, the calendar password, your Google calendar's secret address, your ntfy
-- topic) are sealed with a key only Beacon holds (data/beacon/vault.key): the orchestrator can see these rows
-- but can't open them.
CREATE TABLE IF NOT EXISTS beacon_secrets (
    slot        text PRIMARY KEY,                    -- gmail | calendar | google_ics | ntfy
    nonce       bytea NOT NULL,
    box         bytea NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- What isn't secret: your Gmail address, the calendar's user name, ntfy's address, and where reading is up to.
CREATE TABLE IF NOT EXISTS beacon_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Email I've looked at in your inbox (headers and a short snippet, never the whole message; read it live
-- when you ask). Kept a week. What it says is data from its sender, never instructions.
CREATE TABLE IF NOT EXISTS mail_items (
    msg_id       text PRIMARY KEY,                   -- the Message-ID (or a stand-in for one without)
    uid          bigint NOT NULL,                    -- its IMAP UID in the inbox (with uidvalidity)
    uidvalidity  bigint NOT NULL,
    from_addr    text NOT NULL DEFAULT '',
    from_name    text NOT NULL DEFAULT '',
    subject      text NOT NULL DEFAULT '',
    received_at  timestamptz NOT NULL,
    snippet      text NOT NULL DEFAULT '',
    important    boolean NOT NULL DEFAULT false,
    why          text,                               -- why it's important (or not): which rule, or the model
    first_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mail_items_recent ON mail_items (received_at DESC);

-- People you've written to (from your Sent mail): their email counts as important.
CREATE TABLE IF NOT EXISTS mail_contacts (
    addr        text PRIMARY KEY,
    last_at     timestamptz NOT NULL
);

-- The next two weeks of both calendars, refreshed every few minutes, so the digest and the day tape can read
-- them without asking the calendars each time. Each repeating event appears once per time it happens.
CREATE TABLE IF NOT EXISTS calendar_events (
    source      text NOT NULL,                       -- mine (your calendar server) | google
    uid         text NOT NULL,
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz,
    all_day     boolean NOT NULL DEFAULT false,
    title       text NOT NULL DEFAULT '',
    location    text NOT NULL DEFAULT '',
    PRIMARY KEY (source, uid, starts_at)
);
CREATE INDEX IF NOT EXISTS calendar_events_when ON calendar_events (starts_at);
