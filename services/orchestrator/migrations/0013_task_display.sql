-- Phase 7: each display keeps its own panels. A task remembers the display that asked for it
-- (a browser or a node's screen), and its panel events go only there. NULL = nobody in
-- particular asked (a schedule, a terminal, a function's own sub-task): shown on every
-- display except node screens. Expand-only.
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS device integer;
