import requests, time, json
base = 'http://127.0.0.1:5050'
urls = [
    '/api/search?q=arvind&k=3',
    '/api/ask?q=arvind',
    '/api/ask/layered?q=arvind',
    '/api/arc?q=arvind',
    '/api/translate',
    '/api/generate',
]
for u in urls:
    print('---', u)
    try:
        t0 = time.time()
        if u == '/api/translate':
            r = requests.post(base + u, json={'target': 'hi', 'text': 'Hello world'}, timeout=120)
        elif u == '/api/generate':
            r = requests.post(base + u, json={'prompt': 'Write a brief news summary about local elections.', 'k': 3}, timeout=120)
        else:
            r = requests.get(base + u, timeout=120)
        elapsed = time.time() - t0
        print('status', r.status_code, 'elapsed', round(elapsed,2))
        try:
            d = r.json()
            print('keys', list(d.keys())[:20])
            print(json.dumps(d, indent=2, ensure_ascii=False)[:1200])
        except Exception as e:
            print('json parse failed', e)
            print('text:', r.text[:1200])
    except Exception as e:
        print('ERR', e)
    print()
