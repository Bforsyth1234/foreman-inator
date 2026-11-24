-- 004_embeddings (#23): one embedding vector per ticket, for the nearest-neighbour router
-- (routing/knn.py).
--
--   ticket_id    the ticket the vector embeds (one row per ticket; re-embedding replaces it)
--   model        the embedding model's config name. Vectors are compared only with vectors
--                of the same model and dims, so a changed model never meets old vectors;
--                knn.backfill re-embeds accepted tickets whose row has another model.
--   dims         the vector's length, as the model returned it
--   vector       little-endian float32 array, L2-normalised (cosine similarity = dot
--                product); exactly 4 * dims bytes
--   text_sha256  sha256 of the text that was embedded (title + body, bounded); a ticket
--                whose issue text changed since is re-embedded by the backfill
--   created_at   ISO-8601 UTC, like every other time column
CREATE TABLE ticket_embeddings (
    ticket_id   INTEGER PRIMARY KEY REFERENCES tickets (id),
    model       TEXT NOT NULL CHECK (length(model) > 0),
    dims        INTEGER NOT NULL CHECK (dims > 0),
    vector      BLOB NOT NULL CHECK (length(vector) = 4 * dims),
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    created_at  TEXT NOT NULL
) STRICT;

CREATE INDEX ticket_embeddings_model ON ticket_embeddings (model, dims);
