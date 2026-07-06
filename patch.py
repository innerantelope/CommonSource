import pathlib
import re

p = pathlib.Path('Project/app/search_api.py')
content = p.read_text(encoding='utf-8')

content = re.sub(
    r'def get_available_translation_model\(\) -> Optional\[str\]:.*?return \"LOCAL\"',
    'def get_available_translation_model() -> Optional[str]:\\n    \"\"\"Return deep-translator if available, else LOCAL.\"\"\"\\n    if HAS_DEEP_TRANSLATOR:\\n        return \"deep-translator\"\\n    return \"LOCAL\"',
    content,
    flags=re.DOTALL
)

p.write_text(content, encoding='utf-8')
