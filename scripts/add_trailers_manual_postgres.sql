-- PostgreSQL: trailer columns on `films` (manual YouTube JSON + TMDB cache).
-- Idempotent: safe to run multiple times (IF NOT EXISTS).
--
-- --- How to run ---
--
-- A) Docker Compose (from repo root, defaults: user/db = redwood):
--    Get-Content .\scripts\add_trailers_manual_postgres.sql -Raw | docker compose -f docker/docker-compose.yml exec -T postgres psql -U redwood -d redwood
--
--    Or from docker/:
--    Get-Content ..\scripts\add_trailers_manual_postgres.sql -Raw | docker compose exec -T postgres psql -U redwood -d redwood
--
-- B) psql on host (replace host/port/user/db if needed):
--    psql "postgresql://USER:PASSWORD@HOST:5432/DBNAME" -f scripts/add_trailers_manual_postgres.sql
--
-- C) Manual GUI (pgAdmin, DBeaver, Azure Data Studio, etc.):
--    Connect to your Redwood DB, open a query window, paste the ALTER TABLE block below only
--    (skip the comment lines if your tool dislikes them).
--
-- --- Columns added (table: films) ---
--
--   trailers_manual         JSONB      NULL  e.g. [{"key":"11charYtId","name":"…","type":"Trailer"}]
--   trailers_tmdb_cache     JSONB      NULL  same shape; cached TMDB /movie/{id}/videos slice
--   trailers_tmdb_cached_at TIMESTAMP  NULL  UTC time of last TMDB cache write
--
-- Note: The API also runs equivalent ALTERs on startup via init_db/_ensure_films_trailer_columns;
--       this script is for DBA / manual repair when you prefer not to rely on the app.

ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_manual JSONB;
ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_tmdb_cache JSONB;
ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_tmdb_cached_at TIMESTAMP;
