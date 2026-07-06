"""Test Qwen/Ollama connection and model availability."""
import requests
import json

print("=" * 60)
print("TASK 6: Verify Qwen/Ollama Connection")
print("=" * 60)

# Test Ollama connection
print("\n--- Ollama Connection ---")
try:
    response = requests.get("http://localhost:11434/api/tags", timeout=5)
    print(f"Status code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Ollama is running")
        models = data.get("models", [])
        print(f"  Available models: {len(models)}")
        
        # Check for Qwen models
        qwen_models = [m for m in models if "qwen" in m.get("name", "").lower()]
        print(f"  Qwen models: {len(qwen_models)}")
        for m in qwen_models:
            print(f"    - {m.get('name')}")
            
        # Check specific models from config
        from core.config import GENERATION_MODELS
        print(f"\n  Checking configured models:")
        for model in GENERATION_MODELS[:5]:
            available = any(model in m.get("name", "") for m in models)
            print(f"    {model}: {'✓' if available else '✗'}")
    else:
        print(f"✗ Ollama returned {response.status_code}")
        print(f"  Response: {response.text}")
        
except Exception as e:
    print(f"✗ Ollama connection failed: {e}")

# Test generation endpoint
print("\n--- Test Generation ---")
try:
    payload = {
        "model": "qwen2.5:1.5b",
        "prompt": "Say hello in one word.",
        "stream": False,
        "options": {"num_predict": 10}
    }
    response = requests.post(
        "http://localhost:11434/api/generate",
        json=payload,
        timeout=30
    )
    print(f"Status code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Generation successful")
        print(f"  Response: {data.get('response', '')[:100]}")
    else:
        print(f"✗ Generation failed with {response.status_code}")
        print(f"  Response: {response.text}")
        
except Exception as e:
    print(f"✗ Generation request failed: {e}")

# Test API models endpoint
print("\n--- API /api/models Endpoint ---")
try:
    response = requests.get("http://localhost:5050/api/models", timeout=10)
    print(f"Status code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✓ API models endpoint working")
        print(f"  Response: {json.dumps(data, indent=2)}")
    else:
        print(f"✗ API models endpoint returned {response.status_code}")
        print(f"  Response: {response.text}")
        
except Exception as e:
    print(f"✗ API models endpoint failed: {e}")

print("\n" + "=" * 60)
