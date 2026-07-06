"""Test retrieval pipeline with debug logging."""
import sys
sys.path.insert(0, r'c:\Users\Ayush\Documents\Project_D\Project\app')

from embed import embed_query
from retrieval.pipeline import retrieve_sources
from retrieval.qdrant_store import is_qdrant_available, get_client

print("=" * 60)
print("TASK 2: Verify Retrieval Pipeline")
print("=" * 60)

# Test query
query = "arvind"
print(f"\nQuery: {query}")

# 1. Test embedding generation
print("\n--- Embedding Generation ---")
try:
    query_vec = embed_query(query)
    if query_vec:
        print(f"✓ Embedding generated successfully")
        print(f"  Embedding dimensions: {len(query_vec)}")
        print(f"  First 5 values: {query_vec[:5]}")
    else:
        print(f"✗ Embedding generation failed - returned None")
except Exception as e:
    print(f"✗ Embedding generation failed with error: {e}")

# 2. Test Qdrant connection
print("\n--- Qdrant Connection ---")
try:
    qdrant_available = is_qdrant_available()
    print(f"Qdrant available: {qdrant_available}")
    if qdrant_available:
        client = get_client()
        if client:
            collections = client.get_collections()
            print(f"✓ Qdrant connected")
            print(f"  Collections: {[c.name for c in collections.collections]}")
            from core.config import QDRANT_COLLECTION
            if QDRANT_COLLECTION in [c.name for c in collections.collections]:
                collection_info = client.get_collection(QDRANT_COLLECTION)
                print(f"  Collection '{QDRANT_COLLECTION}' exists")
                print(f"  Vector count: {collection_info.points_count}")
                print(f"  Vector dimensions: {collection_info.config.params.vectors.size}")
            else:
                print(f"✗ Collection '{QDRANT_COLLECTION}' not found")
        else:
            print(f"✗ Qdrant client is None")
    else:
        print(f"✗ Qdrant not available")
except Exception as e:
    print(f"✗ Qdrant connection failed: {e}")

# 3. Test full retrieval pipeline
print("\n--- Full Retrieval Pipeline ---")
try:
    result = retrieve_sources(query, top_k=8)
    print(f"✓ Retrieval executed")
    print(f"  Query: {result.get('query')}")
    print(f"  Count: {result.get('count')}")
    print(f"  Backend: {result.get('retrieval_backend')}")
    print(f"  Results: {len(result.get('results', []))}")
    
    if result.get('results'):
        print(f"\n  Sample result:")
        first = result['results'][0]
        print(f"    Title: {first.get('title', '')[:60]}...")
        print(f"    Publication: {first.get('publication')}")
        print(f"    Score: {first.get('score')}")
        print(f"    Excerpt: {first.get('excerpt', '')[:100]}...")
    else:
        print(f"  No results returned")
        
    if result.get('error'):
        print(f"  Error: {result.get('error')}")
except Exception as e:
    print(f"✗ Retrieval pipeline failed: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
