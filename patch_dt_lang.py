import pathlib
import re
p = pathlib.Path('Project/app/search_api.py')
c = p.read_text(encoding='utf-8')
c = c.replace('target=target_language', 'target=target_language.lower()')
p.write_text(c, encoding='utf-8')
print('Patched target_language.lower()!')
