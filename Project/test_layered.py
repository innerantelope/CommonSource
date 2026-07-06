"""Test Layered Answers endpoint."""
import requests
import json
import time

print("=" * 60)
print("TASK 7: Verify Layered Answers Endpoint")
print("=" * 60)

# Test layered answers endpoint
query = "arvind"
url = f"http://localhost:5050/api/ask/layered?q={query}"

print(f"\nTesting: {url}")
print("Note: This endpoint may take 1-2 minutes due to LLM generation")

start_time = time.time()

try:
    response = requests.get(url, timeout=180)
    elapsed = time.time() - start_time
    
    print(f"\nStatus code: {response.status_code}")
    print(f"Elapsed time: {elapsed:.1f}s")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✓ API returned 200")
        print(f"  Query: {data.get('query')}")
        print(f"  Model: {data.get('model')}")
        
        layers = data.get('layers', {})
        print(f"  Layers: {len(layers)}")
        
        for layer_name, layer_data in layers.items():
            summary = layer_data.get('summary', '')[:100]
            sources_count = len(layer_data.get('sources', []))
            print(f"    {layer_name}: {sources_count} sources, summary: {summary}...")
        
        gaps = data.get('gaps', '')
        if gaps:
            print(f"  Gaps: {gaps[:100]}...")
        
        all_sources = data.get('all_sources', [])
        print(f"  Total sources: {len(all_sources)}")
        
        if data.get('warning'):
            print(f"  Warning: {data.get('warning')}")
    else:
        print(f"✗ API returned {response.status_code}")
        print(f"  Response: {response.text[:500]}")
        
except requests.exceptions.Timeout:
    print(f"✗ Request timed out after 180s")
except Exception as e:
    print(f"✗ Request failed: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
