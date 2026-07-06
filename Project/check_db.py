import sqlite3
from pathlib import Path

DB_PATH = Path(r'c:\Users\Ayush\Documents\Project_D\Project\data\database\commonsource.db')

print(f"Database path: {DB_PATH}")
print(f"Database exists: {DB_PATH.exists()}")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Check tables
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
tables = cursor.fetchall()
print(f"\nTables: {[t[0] for t in tables]}")

# Check article count
cursor.execute('SELECT COUNT(*) FROM commonsource_articles')
articles = cursor.fetchone()
print(f"Articles: {articles[0]}")

# Check chunk count
cursor.execute('SELECT COUNT(*) FROM knowledge_chunks')
chunks = cursor.fetchone()
print(f"Total chunks: {chunks[0]}")

# Check embedded chunks
cursor.execute('SELECT COUNT(*) FROM knowledge_chunks WHERE embedding_blob IS NOT NULL')
embedded = cursor.fetchone()
print(f"Embedded chunks: {embedded[0]}")

# Check sample data
cursor.execute('SELECT asset_id, article_title, publication FROM commonsource_articles LIMIT 3')
sample_articles = cursor.fetchall()
print(f"\nSample articles: {len(sample_articles)}")
for row in sample_articles:
    print(f"  - {row[0]}: {row[1][:50]}... ({row[2]})")

cursor.execute('SELECT id, asset_id, chunk_text FROM knowledge_chunks LIMIT 3')
sample_chunks = cursor.fetchall()
print(f"\nSample chunks: {len(sample_chunks)}")
for row in sample_chunks:
    print(f"  - {row[0]}: {row[1]} - {row[2][:50]}...")

conn.close()
