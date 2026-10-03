-- Phase 7: notifications on his phone (Web Push). A display that turned notifications on keeps its
-- push subscription here (the push service's address for it and the keys that encrypt what's sent);
-- it goes with the device. Her own signing key for push (VAPID) is made once and kept on the server.
-- Expand-only.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          serial PRIMARY KEY,
    device_id   integer NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    endpoint    text NOT NULL UNIQUE,
    p256dh      text NOT NULL,
    auth        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_ok     timestamptz,
    failures    integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS push_subscriptions_device ON push_subscriptions (device_id);

CREATE TABLE IF NOT EXISTS server_secrets (
    name        text PRIMARY KEY,
    value       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
