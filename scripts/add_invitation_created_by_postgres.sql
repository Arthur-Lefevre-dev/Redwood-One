-- PostgreSQL: link member-generated invite codes to users (history in Paramètres).
-- Applied on API startup via init_db(); optional manual run for DBAs.

ALTER TABLE invitation_codes ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_invitation_codes_created_by_user_id ON invitation_codes(created_by_user_id);
