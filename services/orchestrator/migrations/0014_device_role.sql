-- Phase 7: what each device is for (presentation, workstation, server, companion, room). NULL =
-- not chosen yet: guessed from what the device reports (app/roles.py). Set only from the
-- devices panel; Aurelia can see roles but never change them. Expand-only.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS role text;
