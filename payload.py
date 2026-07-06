import pathlib
import re

p = pathlib.Path("Project/app/search_api.py")
content = p.read_text(encoding="utf-8")

new_get_llm = """def get_llm_model() -> str:
    import os
    model = os.getenv("COMMONSOURCE_LLM_MODEL", "gemma3:4b")
    log.info("[LLM] Using model: %s", model)
    try:
        from flask import g
        g.active_model = model
    except Exception:
        pass
    return model"""
line_to_find = r"def get_llm_model\(\) -> str:.*?return model"
before = len(content)
content = re.sub(line_to_find, new_get_llm, content, flags=re.DOTALL)
hooks = """
@app.before_request
def start_request_timer():
    from flask import g
    import time
    g.start_time = time.time()

@app.after_request
def log_request_metrics(response):
    from flask import g, request
    import time
    if hasattr(g, 'start_time') and request.endpoint in ('ask', 'ask_layered', 'arc', 'generate', 'model_test'):
        latency = time.time() - g.start_time
        model = getattr(g, 'active_model', 'unknown')
        status = response.status_code
        timeout_event = status == 504
        log.info(d"[{request.endpoint}] active_model={model} latency={latency:.3f}s timeout_event={timeout_event}")
    return response
"""
content = content.replace("log = logging.getLogger(__name__)\n", "log = logging.getLogger(__name__)\n" + hooks)

p.write_text(content, encoding="utf-8")
print("Patched logging hooks!")