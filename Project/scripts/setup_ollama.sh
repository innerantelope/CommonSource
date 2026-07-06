#!/usr/bin/env bash
# CommonSource — install local Qwen via Ollama (macOS/Linux)
set -euo pipefail

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is not installed. Get it from https://ollama.com/download"
  exit 1
fi

echo "Pulling fast Qwen model (qwen2.5:1.5b)..."
ollama pull qwen2.5:1.5b

echo "Optional: embedding model for ingest..."
ollama pull nomic-embed-text

echo ""
ollama list
echo ""
echo "Start API: cd Project/app && python3 search_api.py"
echo "Open http://localhost:5050"
