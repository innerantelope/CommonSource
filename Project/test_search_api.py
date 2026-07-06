"""Test Search API endpoint."""
import requests
import json

print("=" * 60)
print("TASK 5: Verify Search API Endpoint")
print("=" * 60)

# Test search endpoint
query = "arvind"
url = f"http://localhost:5050/api/search?q={query}&k=8"

print(f"\nTesting: {url}")

try:
    response = requests.get(url, timeout=30)
    print(f"Status code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✓ API returned 200")
        print(f"  Query: {data.get('query')}")
        print(f"  Count: {data.get('count')}")
        print(f"  Results: {len(data.get('results', []))}")
        
        if data.get('results'):
            print(f"\n  Sample results:")
            for i, r in enumerate(data['results'][:3], 1):
                print(f"    {i}. {r.get('title', '')[:60]}...")
                print(f"       Score: {r.get('score')}")
                print(f"       Publication: {r.get('publication')}")
        else:
            print(f"  No results returned")
            
        if data.get('error'):
            print(f"  Error: {data.get('error')}")
    else:
        print(f"✗ API returned {response.status_code}")
        print(f"  Response: {response.text}")
        
except Exception as e:
    print(f"✗ Request failed: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
