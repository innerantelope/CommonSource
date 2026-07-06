FROM python:3.12-slim AS runtime

ARG PYTORCH_VERSION=2.5.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    COMMONSOURCE_PORT=5050 \
    COMMONSOURCE_USE_QDRANT=1 \
    QDRANT_URL=http://qdrant:6333 \
    XDG_CACHE_HOME=/opt/commonsource/.cache \
    HF_HOME=/opt/commonsource/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/opt/commonsource/.cache/sentence-transformers

WORKDIR /opt/commonsource/Project

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system commonsource \
    && useradd --system --gid commonsource --home-dir /opt/commonsource commonsource

COPY app/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip wheel \
    && python -m pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch==${PYTORCH_VERSION}+cpu" \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY scripts ./scripts

RUN mkdir -p \
      data/database \
      data/imports \
      data/email_outbox \
      data/cache \
      data/security \
      /opt/commonsource/.cache \
    && chown -R commonsource:commonsource /opt/commonsource

USER commonsource
WORKDIR /opt/commonsource/Project/app

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=5).read()" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "8", "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", "search_api:app"]
