-- PostgreSQL: add series / content_kind columns to an existing `films` table.
-- Bash (repo root): docker compose -f docker/docker-compose.yml exec -T postgres psql -U redwood -d redwood -f - < scripts/add_series_columns_postgres.sql
-- PowerShell (from docker/): Get-Content ..\scripts\add_series_columns_postgres.sql -Raw | docker compose exec -T postgres psql -U redwood -d redwood
-- Skip if you use a fresh DB created after these fields existed in models.

DO $$ BEGIN
  CREATE TYPE contentkind AS ENUM ('film', 'series_episode');
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE films
  ADD COLUMN IF NOT EXISTS content_kind contentkind NOT NULL DEFAULT 'film';
ALTER TABLE films ADD COLUMN IF NOT EXISTS series_key VARCHAR(160);
ALTER TABLE films ADD COLUMN IF NOT EXISTS series_title VARCHAR(512);
ALTER TABLE films ADD COLUMN IF NOT EXISTS season_number INTEGER;
ALTER TABLE films ADD COLUMN IF NOT EXISTS episode_number INTEGER;

CREATE INDEX IF NOT EXISTS ix_films_series_key ON films (series_key);
CREATE INDEX IF NOT EXISTS ix_films_series_season_ep ON films (series_key, season_number, episode_number);
