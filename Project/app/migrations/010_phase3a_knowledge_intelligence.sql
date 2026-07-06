CREATE TABLE IF NOT EXISTS document_entities (
    document_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    mentions INTEGER NOT NULL DEFAULT 1,
    extraction_method TEXT NOT NULL DEFAULT 'rule',
    created_at TEXT NOT NULL,
    PRIMARY KEY(document_id, entity_id),
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE,
    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_entities_document ON document_entities(document_id);
CREATE INDEX IF NOT EXISTS idx_document_entities_entity ON document_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_document_entities_type ON document_entities(entity_type);

CREATE TABLE IF NOT EXISTS document_citations (
    document_id TEXT PRIMARY KEY,
    apa TEXT NOT NULL DEFAULT '',
    mla TEXT NOT NULL DEFAULT '',
    chicago TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_citations_updated ON document_citations(updated_at);
