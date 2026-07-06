import requests, time, json
for ep, meth, body in [
    ('/api/ask?q=arvind', 'GET', None),
]:
    print('---', meth, ep)
    t0=time.time()
    try:
        if meth=='GET':
            r=requests.get('http://127.0.0.1:5050'+ep, timeout=120)
        else:
            r=requests.post('http://127.0.0.1:5050'+ep, json=body, timeout=120)
        print('status', r.status_code, 'elapsed', round(time.time()-t0,2))
        try:
            d=r.json()
            print('keys', list(d.keys())[:20])
            print(json.dumps(d, indent=2, ensure_ascii=False)[:1200])
        except Exception as e:
            print('json parse failed', e)
            print(r.text[:1200])
    except Exception as e:
        print('ERR', e)
