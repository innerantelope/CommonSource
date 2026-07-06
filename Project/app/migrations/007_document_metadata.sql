CREATE TABLE IF NOT EXISTS document_metadata (
    document_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    import_date TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_metadata_language ON document_metadata(language);
CREATE INDEX IF NOT EXISTS idx_document_metadata_type ON document_metadata(document_type);
CREATE INDEX IF NOT EXISTS idx_document_metadata_hash ON document_metadata(content_hash);

CREATE TABLE IF NOT EXISTS document_categories (
    document_id TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.5,
    matched_terms_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(document_id, category),
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_categories_category ON document_categories(category);

CREATE TABLE IF NOT EXISTS document_tags (
    document_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.5,
    category TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(document_id, tag),
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);

CREATE TABLE IF NOT EXISTS document_keywords (
    document_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY(document_id, keyword),
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_keywords_keyword ON document_keywords(keyword);

CREATE TABLE IF NOT EXISTS bulk_import_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    publisher_id TEXT NOT NULL DEFAULT '',
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    duplicate_files INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS bulk_import_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    asset_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES bulk_import_jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bulk_items_job_status ON bulk_import_items(job_id, status);
CREATE INDEX IF NOT EXISTS idx_bulk_items_hash ON bulk_import_items(content_hash);
