# CommonSource on Oracle Cloud Free Tier

This deployment keeps the CommonSource architecture intact:

```text
Nginx HTTPS
  -> Gunicorn + Flask on 127.0.0.1:5050
  -> SQLite and uploads on VM disk
  -> Qdrant in Docker on localhost:6333
  -> Gemini Flash API for LLM features
```

## Recommended free VM

Use an Oracle Cloud Infrastructure Always Free Ampere A1 VM.

Suggested shape for new Always Free accounts:

- Image: Ubuntu 22.04 or 24.04
- Shape: `VM.Standard.A1.Flex`
- OCPUs: 2
- Memory: 12 GB
- Boot volume: 100 GB

Oracle's Always Free documentation currently lists Ampere A1 compute as 1,500 OCPU hours and 9,000 GB hours per month for Always Free tenancies, equivalent to 2 OCPUs and 12 GB memory when run continuously.

## OCI console setup

1. Create an Oracle Cloud Free Tier account.
2. Create a VM instance in your home region.
3. Choose Ubuntu and `VM.Standard.A1.Flex`.
4. Add your SSH public key.
5. In the VM subnet security list or network security group, allow inbound:
   - TCP 22 from your IP
   - TCP 80 from `0.0.0.0/0`
   - TCP 443 from `0.0.0.0/0`
6. Reserve the public IP if you want it to stay stable.
7. Optional but recommended: create a DNS `A` record pointing your domain/subdomain to the public IP.

## Server install

SSH into the VM:

```bash
ssh ubuntu@YOUR_VM_PUBLIC_IP
```

Install git and clone your repository:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git commonsource
cd commonsource
```

Create the Oracle environment file:

```bash
cp Project/deploy/oracle/commonsource.env.example Project/deploy/oracle/commonsource.env
nano Project/deploy/oracle/commonsource.env
```

Set at minimum:

```text
COMMONSOURCE_PUBLIC_URL=https://commonsource.yourdomain.com
COMMONSOURCE_CORS_ORIGINS=https://commonsource.yourdomain.com
COMMONSOURCE_JWT_SECRET=<64+ character random secret>
GEMINI_API_KEY=<your Gemini key>
SMTP_USERNAME=<your Gmail address>
SMTP_PASSWORD=<your Gmail App Password>
SMTP_FROM=CommonSource <your Gmail address>
```

Generate a JWT secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Run the setup script:

```bash
sudo bash Project/deploy/oracle/setup-ubuntu.sh --domain commonsource.yourdomain.com
```

If you do not have a domain yet, use:

```bash
sudo bash Project/deploy/oracle/setup-ubuntu.sh
```

Then open:

```text
http://YOUR_VM_PUBLIC_IP
```

## HTTPS

After your DNS record points to the VM:

```bash
sudo certbot --nginx -d commonsource.yourdomain.com
```

Certbot configures HTTPS and automatic renewal.

## Operations

Check the app:

```bash
sudo systemctl status commonsource --no-pager
sudo journalctl -u commonsource -f
```

Restart after code changes:

```bash
git pull
sudo systemctl restart commonsource
```

Check Qdrant:

```bash
docker ps
curl http://127.0.0.1:6333/readyz
```

Check CommonSource:

```bash
curl "http://127.0.0.1:5050/api/search?q=test&k=1"
curl "http://127.0.0.1:5050/api/llm/health"
curl "http://127.0.0.1:5050/api/qdrant/health"
curl "http://127.0.0.1:5050/api/email/health"
```

## Data migration from local machine

Stop the remote app:

```bash
sudo systemctl stop commonsource
```

Copy local data to the VM:

```bash
scp -r Project/data/database ubuntu@YOUR_VM_PUBLIC_IP:~/commonsource/Project/data/
scp -r Project/data/imports ubuntu@YOUR_VM_PUBLIC_IP:~/commonsource/Project/data/
```

On the VM:

```bash
sudo chown -R commonsource:commonsource ~/commonsource/Project/data
sudo systemctl start commonsource
```

For Qdrant, rebuild from SQLite using existing diagnostics/admin rebuild tools, or copy a Qdrant snapshot if you already created one.

## Free-tier limits

- Keep Gemini as the LLM provider; do not run a large local LLM on the free VM.
- Keep Gunicorn at 1 worker with multiple threads to save memory.
- Use the local Qdrant Docker volume for demos and testing.
- Back up `Project/data/database` and `Project/data/imports`.
- Oracle capacity can vary by region; if Ampere creation fails, try another availability domain or retry later.
