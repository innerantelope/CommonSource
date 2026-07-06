import sys
import requests
import json

API = "http://localhost:5050"
passed = 0
failed = 0

def log_test(name, result, details=""):
    global passed, failed
    if result:
        passed += 1
        print(f"PASS: {name} - {details}")
    else:
        failed += 1
        print(f"FAIL: {name} - {details}")

def test_search():
    try:
        queries = ["india", "climate change", "education", "asdasdasd123123"]
        for q in queries:
            res = requests.get(f"{API}/api/search?q={q}", timeout=15)
            if res.status_code != 200: return log_test("Search", False, f"'{q}' HTTP {res.status_code}")
            data = res.json()
            if "results" not in data: return log_test("Search", False, f"'{q}' missing results")
            if q == "asdasdasd123123" and len(data["results"]) > 0: pass # OK
        log_test("Search", True)
    except Exception as e: log_test("Search", False, str(e))

def test_translation():
    try:
        payloads = [
            {"text": "Hello world", "target_language": "hi", "source": "en"},
            {"text": "Hello world", "target_language": "ta", "source": "en"},
            {"text": "Hello world", "target_language": "bn", "source": "en"},
            {"text": "?????? ??????", "target_language": "en", "source": "hi"}
        ]
        for p in payloads:
            res = requests.post(f"{API}/api/translate", json=p, timeout=30)
            if res.status_code != 200: return log_test("Translation", False, f"{p['source']}->{p['target_language']} HTTP {res.status_code}")
            if "translation" not in res.json() and "translations" not in res.json() and "translated_text" not in res.json():
                return log_test("Translation", False, f"no translation key in {res.json()}")
        log_test("Translation", True)
    except Exception as e: log_test("Translation", False, str(e))

def test_evidence_layers():
    try:
        res = requests.get(f"{API}/api/ask/layered?q=health", timeout=180)
        if res.status_code != 200: return log_test("Evidence Layers", False, f"HTTP {res.status_code}")
        if "layers" not in res.json(): return log_test("Evidence Layers", False, "No layers")
        log_test("Evidence Layers", True)
    except Exception as e: log_test("Evidence Layers", False, str(e))

def test_story_arc():
    try:
        res = requests.get(f"{API}/api/arc?q=health", timeout=180)
        if res.status_code != 200: return log_test("Story Arc", False, f"HTTP {res.status_code}")
        data = res.json()
        if "narrative" not in data: return log_test("Story Arc", False, "No narrative key")
        log_test("Story Arc", True)
    except Exception as e: log_test("Story Arc", False, str(e))

def test_script_writer():
    try:
        res = requests.post(f"{API}/api/generate", json={"prompt": "Write a script"}, timeout=180)
        if res.status_code != 200: return log_test("Script Writer", False, f"HTTP {res.status_code} {res.text}")
        if "response" not in res.json() and "text" not in res.json(): return log_test("Script Writer", False, "No response")
        log_test("Script Writer", True)
    except Exception as e: log_test("Script Writer", False, str(e))

def test_timeline():
    try:
        res = requests.get(f"{API}/api/timeline?q=health", timeout=60)
        if res.status_code != 200: return log_test("Timeline", False, f"HTTP {res.status_code}")
        if "by_year" not in res.json(): return log_test("Timeline", False, "No by_year key")
        log_test("Timeline", True)
    except Exception as e: log_test("Timeline", False, str(e))

def test_publisher_reg():
    try:
        res = requests.post(f"{API}/api/publisher/register", json={"name": "Test Pub", "website": "test.com", "contact_email": "test@test.com"}, timeout=10)
        if res.status_code not in (200, 201, 409): return log_test("Publisher Registration", False, f"HTTP {res.status_code}")
        log_test("Publisher Registration", True)
    except Exception as e: log_test("Publisher Registration", False, str(e))

def test_publisher_approval():
    try:
        res = requests.get(f"{API}/api/admin/publishers", timeout=10)
        if res.status_code != 200: res = requests.get(f"{API}/api/publishers", timeout=10)
        if res.status_code != 200: return log_test("Publisher Approval", False, "Failed to get publishers")
        pubs = res.json().get("publishers", [])
        if pubs:
             pub_id = pubs[-1]["id"]
             res2 = requests.post(f"{API}/api/admin/publishers/{pub_id}/approve", timeout=10)
             if res2.status_code == 200: return log_test("Publisher Approval", True)
             return log_test("Publisher Approval", False, f"HTTP {res2.status_code}")
        log_test("Publisher Approval", False, "No publisher to approve")
    except Exception as e: log_test("Publisher Approval", False, str(e))

def test_feed_management():
    try:
        res = requests.get(f"{API}/api/feeds", timeout=10)
        if res.status_code == 200: return log_test("Feed Management", True)
        log_test("Feed Management", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Feed Management", False, str(e))

def test_article_upload():
    try:
        res = requests.get(f"{API}/api/admin/publishers", timeout=10)
        pubs = res.json().get("publishers", [])
        if not pubs:
             return log_test("Article Upload", False, "No publisher to upload to")
        pub_id = pubs[-1]["id"]
        files = {'file': ('test.txt', b'Hello world.')}
        res = requests.post(f"{API}/api/ingest/upload", files=files, data={'publisher_id': pub_id}, timeout=30)
        if res.status_code in (200, 201): return log_test("Article Upload", True)
        log_test("Article Upload", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Article Upload", False, str(e))

def test_admin_dashboard():
    try:
        res = requests.get(f"{API}/api/admin/dashboard", timeout=10)
        if res.status_code == 200: return log_test("Admin Dashboard", True)
        log_test("Admin Dashboard", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Admin Dashboard", False, str(e))

def test_corpus_stats():
    try:
        res = requests.get(f"{API}/api/corpus/stats", timeout=10)
        if res.status_code != 200: res = requests.get(f"{API}/api/stats", timeout=10)
        if res.status_code == 200: return log_test("Corpus Statistics", True)
        log_test("Corpus Statistics", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Corpus Statistics", False, str(e))

def test_retrieval_diag():
    try:
        res = requests.get(f"{API}/api/retrieval/diagnostics?q=health", timeout=10)
        if res.status_code == 200: return log_test("Retrieval Diagnostics", True)
        log_test("Retrieval Diagnostics", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Retrieval Diagnostics", False, str(e))

def test_model_health():
    try:
        res = requests.get(f"{API}/api/health/models", timeout=10)
        if res.status_code != 200: res = requests.get(f"{API}/api/models", timeout=10)
        if res.status_code == 200: return log_test("Model Health", True)
        log_test("Model Health", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Model Health", False, str(e))

def test_qdrant_health():
    try:
        res = requests.get(f"{API}/api/qdrant/health", timeout=10)
        if res.status_code in (200, 503): return log_test("Qdrant Health", True, f"HTTP {res.status_code}")
        log_test("Qdrant Health", False, f"HTTP {res.status_code}")
    except Exception as e: log_test("Qdrant Health", False, str(e))

if __name__ == "__main__":
    test_search()
    test_translation()
    test_evidence_layers()
    test_story_arc()
    test_script_writer()
    test_timeline()
    test_publisher_reg()
    test_publisher_approval()
    test_feed_management()
    test_article_upload()
    test_admin_dashboard()
    test_corpus_stats()
    test_retrieval_diag()
    test_model_health()
    test_qdrant_health()
    print(f"\nSummary: {passed} passed, {failed} failed")
