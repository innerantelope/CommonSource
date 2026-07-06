import sys
import os
import requests
import json
import sqlite3
import time

API = "http://localhost:5050"
DB_PATH = "data/database/commonsource.db"

# User Credentials
TEST_EMAIL = "test_runner_admin@example.com"
TEST_PASS = "RunnerPass123!"
TEST_NAME = "Test Runner Admin"

headers = {}
csrf_token = ""
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

def run_db_query(query, params=()):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"Database error: {e}")
        return []

def setup_authentication():
    global headers, csrf_token
    print("Setting up authentication...")

    # 1. Register user (OTP is disabled on server)
    reg_payload = {"name": TEST_NAME, "email": TEST_EMAIL, "password": TEST_PASS}
    try:
        res = requests.post(f"{API}/api/auth/register", json=reg_payload, timeout=10)
        if res.status_code == 409:
            print("  User already registered, proceeding...")
        elif res.status_code not in (200, 201):
            print(f"  Failed to register user: HTTP {res.status_code} {res.text}")
            return False
    except Exception as e:
        print(f"  Registration request failed: {e}")
        return False

    # 2. Elevate user to super_admin in SQLite database
    run_db_query("UPDATE users SET role = 'super_admin' WHERE email = ?", (TEST_EMAIL,))
    print("  User elevated to super_admin in database.")

    # 3. Log in to get tokens
    login_payload = {"email": TEST_EMAIL, "password": TEST_PASS}
    try:
        res = requests.post(f"{API}/api/auth/login", json=login_payload, timeout=10)
        if res.status_code != 200:
            print(f"  Login failed: HTTP {res.status_code} {res.text}")
            return False
        tokens = res.json()
        access_token = tokens.get("access_token")
        if not access_token:
            print("  Access token missing in login response.")
            return False
        headers = {"Authorization": f"Bearer {access_token}"}
        print("  Login successful. Bearer token set.")
    except Exception as e:
        print(f"  Login request failed: {e}")
        return False

    # 4. Fetch CSRF token
    try:
        res = requests.get(f"{API}/api/auth/csrf", headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"  Failed to get CSRF token: HTTP {res.status_code}")
            return False
        csrf_token = res.json().get("csrf_token")
        print("  CSRF token retrieved successfully.")
        return True
    except Exception as e:
        print(f"  CSRF retrieval failed: {e}")
        return False

def test_search():
    try:
        queries = ["india", "climate change", "asdasdasd123123"]
        for q in queries:
            res = requests.get(f"{API}/api/search?q={q}", timeout=15)
            if res.status_code != 200:
                return log_test("Search", False, f"'{q}' HTTP {res.status_code}")
            data = res.json()
            if "results" not in data:
                return log_test("Search", False, f"'{q}' missing results")
        log_test("Search", True)
    except Exception as e:
        log_test("Search", False, str(e))

def test_translation():
    try:
        payloads = [
            {"text": "Hello world", "target_language": "hi", "source": "en"},
            {"text": "Hello world", "target_language": "ta", "source": "en"},
            {"text": "Hello world", "target_language": "bn", "source": "en"},
            {"text": "नमस्ते दुनिया", "target_language": "en", "source": "hi"}
        ]
        for p in payloads:
            res = requests.post(f"{API}/api/translate", json=p, timeout=30)
            if res.status_code != 200:
                return log_test("Translation", False, f"{p['source']}->{p['target_language']} HTTP {res.status_code} {res.text}")
            data = res.json()
            if "translated" not in data and "translation" not in data and "translated_text" not in data:
                return log_test("Translation", False, f"no translation keys found in {data}")
        log_test("Translation", True)
    except Exception as e:
        log_test("Translation", False, str(e))

def test_evidence_layers():
    try:
        res = requests.get(f"{API}/api/ask/layered?q=health", headers=headers, timeout=180)
        if res.status_code != 200:
            return log_test("Evidence Layers", False, f"HTTP {res.status_code} {res.text}")
        if "layers" not in res.json():
            return log_test("Evidence Layers", False, "No layers key")
        log_test("Evidence Layers", True)
    except Exception as e:
        log_test("Evidence Layers", False, str(e))

def test_story_arc():
    try:
        res = requests.get(f"{API}/api/arc?q=health", headers=headers, timeout=180)
        if res.status_code != 200:
            return log_test("Story Arc", False, f"HTTP {res.status_code} {res.text}")
        data = res.json()
        if "story" not in data and "response" not in data and "narrative" not in data:
            return log_test("Story Arc", False, f"No story/narrative keys found in {data}")
        log_test("Story Arc", True)
    except Exception as e:
        log_test("Story Arc", False, str(e))

def test_script_writer():
    try:
        payload = {"prompt": "Write a short radio script about health in villages."}
        res = requests.post(f"{API}/api/generate", json=payload, headers=headers, timeout=180)
        if res.status_code != 200:
            return log_test("Script Writer", False, f"HTTP {res.status_code} {res.text}")
        data = res.json()
        if "response" not in data and "text" not in data:
            return log_test("Script Writer", False, f"No response/text keys found in {data}")
        log_test("Script Writer", True)
    except Exception as e:
        log_test("Script Writer", False, str(e))

def test_timeline():
    try:
        res = requests.get(f"{API}/api/timeline?q=health", timeout=60)
        if res.status_code != 200:
            return log_test("Timeline", False, f"HTTP {res.status_code}")
        data = res.json()
        if "by_year" not in data and "events" not in data:
            return log_test("Timeline", False, "No by_year or events key")
        log_test("Timeline", True)
    except Exception as e:
        log_test("Timeline", False, str(e))

def test_publisher_reg():
    try:
        res = requests.post(
            f"{API}/api/publisher/register",
            json={"name": "Test Pub", "website": "test.com", "contact_email": "test@test.com"},
            timeout=10
        )
        if res.status_code not in (200, 201, 409):
            return log_test("Publisher Registration", False, f"HTTP {res.status_code}")
        log_test("Publisher Registration", True)
    except Exception as e:
        log_test("Publisher Registration", False, str(e))

def test_publisher_approval():
    try:
        # Fetch publishers via admin endpoint (requires auth)
        res = requests.get(f"{API}/api/admin/publishers", headers=headers, timeout=10)
        if res.status_code != 200:
            return log_test("Publisher Approval", False, f"Failed to get publishers: HTTP {res.status_code}")

        pubs = res.json().get("publishers", [])
        if pubs:
            # Find a pending publisher or use the last one
            pending_pub = next((p for p in pubs if p.get("status") == "pending"), pubs[-1])
            pub_id = pending_pub["id"]

            res2 = requests.post(f"{API}/api/admin/publishers/{pub_id}/approve", headers={**headers, "X-CSRF-Token": csrf_token}, timeout=10)
            if res2.status_code == 200:
                return log_test("Publisher Approval", True)
            return log_test("Publisher Approval", False, f"Approve request returned HTTP {res2.status_code} {res2.text}")
        else:
            # Register a publisher first so we can approve it
            reg_res = requests.post(
                f"{API}/api/publisher/register",
                json={"name": "Auto Temp Publisher", "website": "temp.com", "contact_email": "temp@temp.com"},
                timeout=10
            )
            if reg_res.status_code in (200, 201):
                # Retrieve it
                res = requests.get(f"{API}/api/admin/publishers", headers=headers, timeout=10)
                pubs = res.json().get("publishers", [])
                if pubs:
                    pub_id = pubs[-1]["id"]
                    res2 = requests.post(f"{API}/api/admin/publishers/{pub_id}/approve", headers={**headers, "X-CSRF-Token": csrf_token}, timeout=10)
                    if res2.status_code == 200:
                        return log_test("Publisher Approval", True, "Registered and approved new publisher")
            log_test("Publisher Approval", False, "No publishers in database and could not auto-create one")
    except Exception as e:
        log_test("Publisher Approval", False, str(e))

def test_feed_management():
    try:
        res = requests.get(f"{API}/api/feeds", headers=headers, timeout=10)
        if res.status_code == 200:
            return log_test("Feed Management", True)
        log_test("Feed Management", False, f"HTTP {res.status_code} {res.text}")
    except Exception as e:
        log_test("Feed Management", False, str(e))

def test_article_upload():
    try:
        # Get approved publishers
        res = requests.get(f"{API}/api/admin/publishers", headers=headers, timeout=10)
        pubs = res.json().get("publishers", [])

        approved_pub = next((p for p in pubs if p.get("status") == "approved"), None)
        if not approved_pub:
            # If no approved publisher, approve one first
            if pubs:
                pub_id = pubs[-1]["id"]
                requests.post(f"{API}/api/admin/publishers/{pub_id}/approve", headers={**headers, "X-CSRF-Token": csrf_token}, timeout=10)
                approved_pub = pubs[-1]
            else:
                return log_test("Article Upload", False, "No publisher available to upload to")

        pub_id = approved_pub["id"]

        # Ingest upload requires CSRF token!
        upload_headers = {**headers, "X-CSRF-Token": csrf_token}
        files = {'file': ('test_file.txt', b'This is a longer test article content from the QA runner that needs to be at least one hundred characters long to bypass the empty or unreadable file validation check on upload.')}

        res = requests.post(
            f"{API}/api/ingest/upload",
            files=files,
            data={'publisher_id': pub_id},
            headers=upload_headers,
            timeout=30
        )
        if res.status_code in (200, 201):
            return log_test("Article Upload", True)
        log_test("Article Upload", False, f"HTTP {res.status_code} {res.text}")
    except Exception as e:
        log_test("Article Upload", False, str(e))

def test_admin_dashboard():
    try:
        res = requests.get(f"{API}/api/admin/dashboard", headers=headers, timeout=10)
        if res.status_code == 200:
            return log_test("Admin Dashboard", True)
        log_test("Admin Dashboard", False, f"HTTP {res.status_code}")
    except Exception as e:
        log_test("Admin Dashboard", False, str(e))

def test_corpus_stats():
    try:
        res = requests.get(f"{API}/api/corpus/stats", timeout=10)
        if res.status_code != 200:
            res = requests.get(f"{API}/api/stats", timeout=10)
        if res.status_code == 200:
            return log_test("Corpus Statistics", True)
        log_test("Corpus Statistics", False, f"HTTP {res.status_code}")
    except Exception as e:
        log_test("Corpus Statistics", False, str(e))

def test_retrieval_diag():
    try:
        res = requests.get(f"{API}/api/retrieval/diagnostics?q=health", timeout=10)
        if res.status_code == 200:
            return log_test("Retrieval Diagnostics", True)
        log_test("Retrieval Diagnostics", False, f"HTTP {res.status_code}")
    except Exception as e:
        log_test("Retrieval Diagnostics", False, str(e))

def test_model_health():
    try:
        res = requests.get(f"{API}/api/health/models", timeout=10)
        if res.status_code != 200:
            res = requests.get(f"{API}/api/models", timeout=10)
        if res.status_code == 200:
            return log_test("Model Health", True)
        log_test("Model Health", False, f"HTTP {res.status_code}")
    except Exception as e:
        log_test("Model Health", False, str(e))

def test_qdrant_health():
    try:
        res = requests.get(f"{API}/api/qdrant/health", timeout=10)
        # 200 if running, 503 is expected since Qdrant is offline locally
        if res.status_code in (200, 503):
            return log_test("Qdrant Health", True, f"HTTP {res.status_code} (offline mode expected)")
        log_test("Qdrant Health", False, f"HTTP {res.status_code}")
    except Exception as e:
        log_test("Qdrant Health", False, str(e))

def cleanup():
    print("Cleaning up test runner admin from database...")
    run_db_query("DELETE FROM users WHERE email = ?", (TEST_EMAIL,))
    print("Cleanup completed.")

if __name__ == "__main__":
    print("=" * 60)
    print("CommonSource Platform Verification Test Suite")
    print("=" * 60)

    if not setup_authentication():
        print("CRITICAL: Failed to configure authentication. Exiting tests.")
        sys.exit(1)

    print("\nRunning Verification Tests...\n")
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

    print("\n" + "=" * 60)
    print(f"Summary: {passed} passed, {failed} failed")
    print("=" * 60)

    cleanup()

    if failed > 0:
        sys.exit(1)
