# CommonSource — install local Qwen via Ollama (Windows)
# Run from repo root:  .\Project\scripts\setup_ollama.ps1

$ErrorActionPreference = "Stop"

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Ollama is not installed."
    Write-Host "Download: https://ollama.com/download"
    exit 1
}

Write-Host "Pulling fast Qwen model for translation and synthesis (qwen2.5:1.5b)..."
ollama pull qwen2.5:1.5b

Write-Host "Optional: embedding model for Ollama-based ingest (nomic-embed-text)..."
ollama pull nomic-embed-text

Write-Host ""
Write-Host "Installed models:"
ollama list

Write-Host ""
Write-Host "Start the CommonSource API:"
Write-Host '  cd Project\app'
Write-Host '  $env:PYTHONIOENCODING="utf-8"; python search_api.py'
Write-Host ""
Write-Host "Then open http://localhost:5050"
