-- PostgreSQL: monthly user invite quota (one generated code per calendar month, UTC).
-- Applied automatically on API startup via init_db(); use this for manual DBA runs.
--
--   docker compose -f docker/docker-compose.yml exec -T postgres psql -U redwood -d redwood -c "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_invite_at TIMESTAMP;"

ALTER TABLE users ADD COLUMN IF NOT EXISTS last_invite_at TIMESTAMP;
