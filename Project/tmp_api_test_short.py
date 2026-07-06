import requests, time, json
base='http://127.0.0.1:5050'
endpoints=[
    ('/api/search?q=arvind&k=3', 'GET'),
    ('/api/ask?q=arvind', 'GET'),
    ('/api/ask/layered?q=arvind', 'GET'),
    ('/api/arc?q=arvind', 'GET'),
    ('/api/translate', 'POST', {'target':'hi','text':'Hello world'}),
    ('/api/generate', 'POST', {'prompt':'Summarize local elections.','k':3}),
]
for ep, method, *body in endpoints:
    print('---', method, ep)
    t0=time.time()
    try:
        if method=='GET':
            r=requests.get(base+ep, timeout=120)
        else:
            r=requests.post(base+ep, json=body[0], timeout=120)
        print('status', r.status_code, 'elapsed', round(time.time()-t0,2))
        data=r.json()
        print('keys', list(data.keys())[:20])
        if 'error' in data:
            print('error', data['error'])
        if 'model' in data:
            print('model', data['model'])
        if 'translation' in data:
            print('translation_snippet', data['translation'][:100])
        if 'narrative' in data:
            print('narrative_snippet', data['narrative'][:100])
    except Exception as e:
        print('ERR', e)
    print()