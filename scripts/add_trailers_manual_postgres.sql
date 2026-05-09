-- PostgreSQL: optional manual YouTube trailers per film (JSON array).
-- PowerShell (from docker/): Get-Content ..\scripts\add_trailers_manual_postgres.sql -Raw | docker compose exec -T postgres psql -U redwood -d redwood

ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_manual JSONB;
