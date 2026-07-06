import pathlib
import re

p = pathlib.Path('Project/app/search_api.py')
content = p.read_text(encoding='utf-8')

new_qwen = '''def translate_with_qwen(
    text: str,
    target_language: str,
    model: str,
    source_language: str = "auto",
    *,
    timeout: Optional[float] = None,
) -> str:
    \"\"\"Translate text using deep-translator.\"\"\"
    text = (text or "").strip()
    if not text:
        return ""
    if not HAS_DEEP_TRANSLATOR or model == "LOCAL":
        lang_code = target_language[:2].lower() if target_language else "en"
        return translate_with_local_model(text, lang_code)
    try:
        if len(text) > 4999:
            text = text[:4999]
        translator = GoogleTranslator(source=source_language, target=target_language)
        return translator.translate(text)
    except Exception as e:
        log.error("deep-translator failed: %s", e)
        return text'''

content = re.sub(
    r'def translate_with_qwen\(.*?return clean_translation_response\(translated\)',
    new_qwen,
    content,
    flags=re.DOTALL
)

new_batch = '''def translate_items_batch(
    items: List[Dict[str, Any]],
    target_language: str,
    model: str,
    source_language: str = "auto",
    *,
    timeout: Optional[float] = None,
) -> List[Dict[str, Any]]:
    \"\"\"Translate multiple source cards sequentially using deep-translator.\"\"\"
    translations = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        item_text = (item.get("text") or "").strip()
        if not item_text:
            translations.append({"id": item_id, "translation": ""})
            continue
        translated = translate_with_qwen(item_text, target_language, model, source_language, timeout=timeout)
        translations.append({"id": item_id, "translation": translated})
    return translations'''

content = re.sub(
    r'def translate_items_batch\(.*?translations\.append\(\{"id": p\["id"\], "translation": block\}\)\n        return translations',
    new_batch,
    content,
    flags=re.DOTALL
)

p.write_text(content, encoding='utf-8')
print("Patched translation functions!")
