# CommonSource Free Demo Deployment

## Recommended option

Use Cloudflare Tunnel to expose the existing local CommonSource machine.

This is the safest free demo path because it keeps SQLite, Qdrant, document uploads, embeddings, and optional Ollama on the same machine where they already work. Cloudflare Tunnel creates outbound-only connections from the local machine to Cloudflare, so no router port forwarding or public server is required.

References:

- Cloudflare Tunnel overview: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/
- Locally managed tunnel config: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/
- Run cloudflared as a service: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/
- Oracle Always Free resources: https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Render free limits: https://render.com/docs/free
- Qdrant Cloud free tier: https://qdrant.tech/pricing/

## Immediate public demo

From the repository root:

```powershell
Copy-Item Project\.env.demo.example Project\.env.demo
notepad Project\.env.demo
```

Set:

```text
COMMONSOURCE_PUBLIC_URL=https://replace-after-tunnel-start
COMMONSOURCE_JWT_SECRET=<64+ character random secret>
COMMONSOURCE_REQUIRE_JWT_SECRET=1
COMMONSOURCE_LLM_PROVIDER=ollama
QDRANT_URL=http://localhost:6333
```

Start Qdrant and CommonSource:

```powershell
cd C:\Users\Ayush\Documents\Project_D
.\Project\deploy\windows\start-commonsource-demo.ps1 -StartQdrant
```

Start a temporary public HTTPS tunnel:

```powershell
cloudflared tunnel --url http://localhost:5050
```

Cloudflare prints a `https://*.trycloudflare.com` URL. Use that URL for demos. Update `COMMONSOURCE_PUBLIC_URL` and `COMMONSOURCE_CORS_ORIGINS` if you want CORS to be strict for that URL.

## Stable public hostname

Use this when you have a Cloudflare-managed domain.

```powershell
cloudflared tunnel login
cloudflared tunnel create commonsource-demo
cloudflared tunnel route dns commonsource-demo commonsource.example.com
```

Copy `Project/deploy/cloudflare/config.yml.example` to your cloudflared config location and replace:

- `YOUR_TUNNEL_ID_OR_NAME`
- `credentials-file`
- `commonsource.example.com`

Run:

```powershell
cloudflared tunnel --config C:\Users\Ayush\.cloudflared\config.yml run commonsource-demo
```

Install automatic startup from the Cloudflare Zero Trust dashboard token:

```powershell
Start-Process powershell -Verb RunAs
.\Project\deploy\windows\install-cloudflared-service.ps1 -TunnelToken "<token from Cloudflare>"
```

Also configure Windows Task Scheduler or NSSM to run:

```powershell
.\Project\deploy\windows\start-commonsource-demo.ps1 -StartQdrant
```

## LLM provider modes

Local Ollama:

```text
COMMONSOURCE_LLM_PROVIDER=ollama
COMMONSOURCE_LLM_MODEL=gemma3:4b
COMMONSOURCE_QWEN_MODEL=qwen2.5:1.5b
OLLAMA_BASE_URL=http://localhost:11434
```

Graceful free-provider fallback:

```text
COMMONSOURCE_LLM_PROVIDER=auto
COMMONSOURCE_LLM_FALLBACK_PROVIDERS=ollama,groq,gemini,openrouter
```

Groq:

```text
COMMONSOURCE_LLM_PROVIDER=groq
GROQ_API_KEY=<key>
COMMONSOURCE_GROQ_MODEL=llama-3.1-8b-instant
```

Gemini:

```text
COMMONSOURCE_LLM_PROVIDER=gemini
GEMINI_API_KEY=<key>
COMMONSOURCE_GEMINI_MODEL=gemini-1.5-flash
```

OpenRouter:

```text
COMMONSOURCE_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=<key>
COMMONSOURCE_OPENROUTER_MODEL=<free model id from OpenRouter>
```

## Qdrant

Local free Qdrant:

```powershell
cd Project
docker compose up -d qdrant
```

Qdrant Cloud free:

```text
QDRANT_URL=https://YOUR_CLUSTER_URL
QDRANT_API_KEY=<qdrant cloud api key>
QDRANT_COLLECTION=commonsource_chunks
```

Then rebuild or sync vectors through the existing Qdrant admin tools.

## Verification

Open:

- `/api/search?q=water&k=2`
- `/api/document/<document_id>`
- `/api/qdrant/health`
- `/api/debug/model-test`
- `/search`
- `/login`
- `/dashboard`

Confirm:

- Search returns results.
- Document reader opens full content.
- Registration/login works.
- Authenticated bookmarks/collections/notes work.
- Evidence Layer, Story Arc, and Script Writer remain paywalled.
- Qdrant health reports available when Qdrant is running.
- Model test reports the selected provider/model or a clear fallback error.

## Known free-demo limitations

- The local machine must stay powered on and online.
- Temporary `trycloudflare.com` URLs change when the tunnel restarts.
- Uploads and SQLite live on the local disk, so backups are your responsibility.
- Local Ollama performance depends on your CPU/GPU.
- Free remote LLM providers have rate limits and model availability can change.
- Render free services are not ideal for this project because SQLite persistence, uploads, Qdrant, and large local embeddings do not fit cleanly into an ephemeral free web service.
- Oracle Always Free can run the stack, but capacity availability varies by region and setup is slower than a local tunnel.

## Paid VPS migration path

1. Provision a small VPS with persistent disk.
2. Copy `Project/data/database/commonsource.db`, `Project/data/imports`, and Qdrant snapshots.
3. Install Python, Docker, cloudflared or Nginx.
4. Use `deploy/systemd/commonsource.service.example`.
5. Run Qdrant with Docker volume or move to Qdrant Cloud.
6. Put Nginx or Cloudflare in front with HTTPS.
7. Set production env vars: `COMMONSOURCE_JWT_SECRET`, `COMMONSOURCE_REQUIRE_JWT_SECRET=1`, `COMMONSOURCE_CORS_ORIGINS`, `QDRANT_URL`, and chosen LLM provider keys.
