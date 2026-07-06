CREATE TABLE IF NOT EXISTS document_translations (
    document_id TEXT NOT NULL,
    target_language TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    translated_title TEXT NOT NULL DEFAULT '',
    translated_content TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(document_id, target_language),
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_translations_language
ON document_translations(target_language);
