import pathlib
p = pathlib.Path('Project/app/search_api.py')
content = p.read_text(encoding='utf-8')

import re
m_ask = re.search(r'(@app\.route\("/api/ask"\)\ndef ask\(\):.*?return jsonify\(\{.*?\}\))', content, re.DOTALL)
if m_ask:
    with open('ask_body.txt', 'w', encoding='utf-8') as f:
        f.write(m_ask.group(1))
