-- Kepler keeps its library whether or not Trilium is reachable, and catches Trilium up later.
-- A topic whose note couldn't be written is marked pending; notes that couldn't be deleted
-- (after a forget) wait here. Both are retried until Trilium answers. Expand-only.
ALTER TABLE kepler_topics ADD COLUMN IF NOT EXISTS trilium_pending boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS kepler_trilium_deletes (
    note_id    text PRIMARY KEY,
    user_id    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
