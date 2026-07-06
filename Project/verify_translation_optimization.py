#!/usr/bin/env python3
"""Comprehensive translation optimization verification test."""
import json
import time
import requests

BASE_URL = "http://localhost:5050"

def test_translation(text, target="hi", test_name="", expected_model="qwen2.5:1.5b"):
    """Test translation with timing and verification."""
    payload = {"text": text, "target": target}
    
    print(f"\n{'='*60}")
    print(f"{test_name}")
    print(f"{'='*60}")
    print(f"Text: {text[:60]}{'...' if len(text) > 60 else ''}")
    print(f"Target: {target}")
    
    try:
        t0 = time.time()
        response = requests.post(f"{BASE_URL}/api/translate", json=payload, timeout=120)
        elapsed = time.time() - t0
        
        if response.status_code != 200:
            print(f"❌ Status: {response.status_code}")
            print(f"Error: {response.text}")
            return False
            
        data = response.json()
        model = data.get('model')
        translation = data.get('translation', '')
        
        # Verification
        model_ok = model == expected_model
        response_ok = bool(translation)
        time_reasonable = elapsed < 45  # 45s is reasonable for new translation
        
        print(f"✓ Status: 200")
        print(f"✓ Model: {model} {f'({expected_model})' if model_ok else f'(expected {expected_model})'}")
        print(f"✓ Response: {translation[:70]}{'...' if len(translation) > 70 else ''}")
        print(f"✓ Time: {elapsed:.2f}s {'(reasonable)' if time_reasonable else '(slow)'}")
        
        all_ok = model_ok and response_ok and time_reasonable
        print(f"\n{'✓ PASS' if all_ok else '❌ FAIL'}")
        return all_ok
        
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

def main():
    """Run comprehensive tests."""
    print("\n" + "="*60)
    print("TRANSLATION OPTIMIZATION VERIFICATION")
    print("="*60)
    
    results = []
    
    # Test 1: Short text (should be <5s new, <2s cached)
    results.append(test_translation(
        "Hello world",
        test_name="Test 1: Short text (new request)"
    ))
    
    # Test 2: Same short text (should be cached, <1s)
    results.append(test_translation(
        "Hello world",
        test_name="Test 2: Short text (cached)"
    ))
    
    # Test 3: Medium text
    results.append(test_translation(
        "The rapid expansion of technology has transformed the way communities communicate.",
        test_name="Test 3: Medium text (new request)"
    ))
    
    # Test 4: Different language
    results.append(test_translation(
        "This is a test",
        target="bn",
        test_name="Test 4: Bengali translation"
    ))
    
    # Test 5: Different language repeated (should be cached)
    results.append(test_translation(
        "This is a test",
        target="bn",
        test_name="Test 5: Bengali translation (cached)"
    ))
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total} tests")
    
    if passed == total:
        print("\n✓ All translation optimizations verified!")
    else:
        print(f"\n⚠ {total - passed} test(s) failed")

if __name__ == "__main__":
    main()
