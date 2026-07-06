CREATE TABLE IF NOT EXISTS source_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE
);

INSERT OR IGNORE INTO source_types (name, slug) VALUES
('news', 'news'),
('report', 'report'),
('research', 'research'),
('magazine', 'magazine'),
('opinion', 'opinion'),
('fact-check', 'fact-check'),
('dataset', 'dataset'),
('other', 'other');

ALTER TABLE commonsource_articles ADD COLUMN source_type_id INTEGER;

UPDATE commonsource_articles
SET source_type_id = (
    SELECT id FROM source_types
    WHERE slug = CASE
        WHEN lower(source_type) IN ('report', 'research', 'magazine', 'opinion', 'fact-check', 'dataset', 'other')
            THEN lower(source_type)
        ELSE 'news'
    END
)
WHERE source_type_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_source_types_slug ON source_types(slug);
CREATE INDEX IF NOT EXISTS idx_cs_articles_source_type_id ON commonsource_articles(source_type_id);
