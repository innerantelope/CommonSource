#!/usr/bin/env python3
"""Test translation endpoint with caching and longer text."""
import json
import time
import requests

BASE_URL = "http://localhost:5050"

def test_translation(text, target="hi", test_name="Translation"):
    """Test translation with timing."""
    payload = {
        "text": text,
        "target": target,
    }
    
    print(f"\n=== {test_name} ===")
    print(f"Text length: {len(text)} chars")
    print(f"Target: {target}")
    
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
            translation = data.get('translation', '')
            print(f"Translation (first 100 chars): {translation[:100]}")
        else:
            print(f"Error: {response.text}")
            
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    # Test 1: Simple text (cached)
    test_translation(
        "Hello world, this is a test message.",
        test_name="Test 1: Simple text"
    )
    
    # Test 2: Same text again (should be cached)
    test_translation(
        "Hello world, this is a test message.",
        test_name="Test 2: Same text (cached)"
    )
    
    # Test 3: Longer text
    longer_text = """The rapid expansion of technology in the early 21st century has fundamentally transformed 
    the way communities communicate and share information. Public broadcasting services have evolved to meet these 
    new demands, adapting their platforms to reach diverse audiences across multiple channels. Community media 
    organizations are now playing an increasingly important role in facilitating meaningful dialogue about public 
    interest issues and social development initiatives."""
    
    test_translation(
        longer_text,
        test_name="Test 3: Longer text"
    )
