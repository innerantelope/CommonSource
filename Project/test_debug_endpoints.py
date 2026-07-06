"""Test debug endpoints."""
import requests
import json

print("=" * 60)
print("Testing Debug Endpoints")
print("=" * 60)

# Test database debug endpoint
print("\n--- /api/debug/db ---")
try:
    response = requests.get("http://localhost:5050/api/debug/db", timeout=10)
    print(f"Status code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Database debug endpoint working")
        print(f"  Articles: {data.get('articles')}")
        print(f"  Chunks: {data.get('chunks')}")
        print(f"  Embedded chunks: {data.get('embedded_chunks')}")
        print(f"  Tables: {len(data.get('tables', []))}")
    else:
        print(f"✗ Error: {response.text}")
except Exception as e:
    print(f"✗ Request failed: {e}")

# Test retrieval debug endpoint
print("\n--- /api/debug/retrieval?q=arvind ---")
try:
    response = requests.get("http://localhost:5050/api/debug/retrieval?q=arvind", timeout=30)
    print(f"Status code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Retrieval debug endpoint working")
        print(f"  Query: {data.get('query')}")
        print(f"  Embedding size: {data.get('embedding_size')}")
        print(f"  Embedding time: {data.get('embedding_time_seconds')}s")
        print(f"  Qdrant available: {data.get('qdrant_available')}")
        print(f"  Retrieval backend: {data.get('retrieval_backend')}")
        print(f"  Retrieval time: {data.get('retrieval_time_seconds')}s")
        print(f"  Total time: {data.get('total_time_seconds')}s")
        print(f"  Final results: {data.get('final_results')}")
        print(f"  Sample results: {len(data.get('sample_results', []))}")
    else:
        print(f"✗ Error: {response.text}")
except Exception as e:
    print(f"✗ Request failed: {e}")

print("\n" + "=" * 60)
