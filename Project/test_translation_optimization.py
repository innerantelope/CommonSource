#!/usr/bin/env python3
"""Test translation endpoint performance after optimization."""
import json
import time
import requests

BASE_URL = "http://localhost:5050"

def test_translation():
    """Test translation with timing."""
    payload = {
        "text": "Hello world, this is a test message.",
        "target": "hi",
    }
    
    print("\n=== Testing /api/translate ===")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    try:
        t0 = time.time()
        response = requests.post(
            f"{BASE_URL}/api/translate",
            json=payload,
            timeout=120
        )
        elapsed = time.time() - t0
        
        print(f"Status: {response.status_code}")
        print(f"Time: {elapsed:.2f}s")
        
        if response.status_code == 200:
            data = response.json()
            print(f"Model: {data.get('model')}")
            print(f"Translation: {data.get('translation')}")
            print(f"Target: {data.get('target')}")
        else:
            print(f"Error: {response.text}")
            
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_translation()
