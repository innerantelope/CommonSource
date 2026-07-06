ALTER TABLE users ADD COLUMN is_paid INTEGER NOT NULL DEFAULT 0 CHECK (is_paid IN (0, 1));
CREATE INDEX IF NOT EXISTS idx_users_paid ON users(is_paid);
