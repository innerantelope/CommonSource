CREATE TABLE IF NOT EXISTS publishers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    geography TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'en',
    contact_email TEXT NOT NULL DEFAULT '',
    storage_mode TEXT NOT NULL DEFAULT 'federated',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publishers_status ON publishers(status);
CREATE INDEX IF NOT EXISTS idx_publishers_email ON publishers(contact_email);

CREATE TABLE IF NOT EXISTS rss_feeds (
    id TEXT PRIMARY KEY,
    publisher_id TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    feed_name TEXT NOT NULL DEFAULT '',
    last_polled_at TEXT,
    last_item_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    FOREIGN KEY(publisher_id) REFERENCES publishers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rss_feeds_publisher ON rss_feeds(publisher_id);
CREATE INDEX IF NOT EXISTS idx_rss_feeds_status ON rss_feeds(status);
CREATE INDEX IF NOT EXISTS idx_rss_feeds_url ON rss_feeds(feed_url);

CREATE TABLE IF NOT EXISTS publisher_profiles (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    organization_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    logo_url TEXT NOT NULL DEFAULT '',
    languages TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '',
    coverage_regions TEXT NOT NULL DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (verification_status IN ('pending', 'verified', 'rejected', 'suspended')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_publisher_profiles_status ON publisher_profiles(verification_status);
CREATE INDEX IF NOT EXISTS idx_publisher_profiles_org ON publisher_profiles(organization_name);

CREATE TABLE IF NOT EXISTS publisher_applications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    organization_name TEXT NOT NULL,
    website TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    review_notes TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(reviewed_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_publisher_applications_user ON publisher_applications(user_id);
CREATE INDEX IF NOT EXISTS idx_publisher_applications_status ON publisher_applications(status);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    reporter_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'resolved', 'dismissed', 'escalated')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(reporter_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(reviewed_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_article ON reports(article_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_id);

CREATE TABLE IF NOT EXISTS moderation_actions (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(report_id) REFERENCES reports(id) ON DELETE CASCADE,
    FOREIGN KEY(actor_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_moderation_actions_report ON moderation_actions(report_id);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_actor ON moderation_actions(actor_id);

ALTER TABLE rss_feeds ADD COLUMN updated_at TEXT;
ALTER TABLE rss_feeds ADD COLUMN deleted_at TEXT;
