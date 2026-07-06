CREATE TABLE IF NOT EXISTS email_otps (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE,
    otp_hash TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('REGISTER', 'PASSWORD_RESET', 'ADMIN_LOGIN')),
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_otps_email_purpose ON email_otps(email, purpose);
CREATE INDEX IF NOT EXISTS idx_email_otps_expires_at ON email_otps(expires_at);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_entity_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at TEXT NOT NULL,
    UNIQUE(normalized_alias, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_canonical ON entity_aliases(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(canonical_entity_id);
