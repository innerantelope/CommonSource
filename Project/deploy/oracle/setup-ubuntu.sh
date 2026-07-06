#!/usr/bin/env bash
set -euo pipefail

DOMAIN="_"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)
      DOMAIN="${2:?Missing domain after --domain}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMONSOURCE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PROJECT_ROOT="$COMMONSOURCE_ROOT/Project"
APP_ROOT="$PROJECT_ROOT/app"
ENV_FILE="$SCRIPT_DIR/commonsource.env"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run with sudo: sudo bash Project/deploy/oracle/setup-ubuntu.sh --domain your.domain" >&2
  exit 1
fi

echo "[CommonSource] Installing OS packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git nginx python3 python3-pip python3-venv \
  docker.io docker-compose-plugin certbot python3-certbot-nginx

echo "[CommonSource] Creating service user..."
if ! id commonsource >/dev/null 2>&1; then
  useradd --system --home "$COMMONSOURCE_ROOT" --shell /usr/sbin/nologin commonsource
fi

echo "[CommonSource] Preparing persistent data directories..."
mkdir -p "$PROJECT_ROOT/data/database" "$PROJECT_ROOT/data/imports" "$PROJECT_ROOT/data/cache" "$PROJECT_ROOT/data/security"
chown -R commonsource:commonsource "$COMMONSOURCE_ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SCRIPT_DIR/commonsource.env.example" "$ENV_FILE"
  chown commonsource:commonsource "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "[CommonSource] Created $ENV_FILE. Edit it with real secrets before starting production traffic."
fi

echo "[CommonSource] Building Python virtualenv..."
python3 -m venv "$COMMONSOURCE_ROOT/.venv"
"$COMMONSOURCE_ROOT/.venv/bin/python" -m pip install --upgrade pip wheel
"$COMMONSOURCE_ROOT/.venv/bin/python" -m pip install gunicorn
"$COMMONSOURCE_ROOT/.venv/bin/python" -m pip install -r "$APP_ROOT/requirements.txt"
chown -R commonsource:commonsource "$COMMONSOURCE_ROOT/.venv"

echo "[CommonSource] Starting Qdrant..."
systemctl enable --now docker
cd "$PROJECT_ROOT"
docker compose up -d qdrant

echo "[CommonSource] Installing systemd service..."
sed "s|__COMMONSOURCE_ROOT__|$COMMONSOURCE_ROOT|g" "$SCRIPT_DIR/commonsource.service" \
  > /etc/systemd/system/commonsource.service
systemctl daemon-reload
systemctl enable commonsource

echo "[CommonSource] Installing Nginx reverse proxy..."
sed "s|__COMMONSOURCE_SERVER_NAME__|$DOMAIN|g" "$SCRIPT_DIR/commonsource-nginx.conf" \
  > /etc/nginx/sites-available/commonsource
ln -sf /etc/nginx/sites-available/commonsource /etc/nginx/sites-enabled/commonsource
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx

echo "[CommonSource] Opening local firewall if ufw exists..."
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH || true
  ufw allow 'Nginx Full' || true
fi

echo "[CommonSource] Starting app..."
systemctl restart commonsource

echo
echo "Done."
echo "Check service: sudo systemctl status commonsource --no-pager"
echo "Check logs:    sudo journalctl -u commonsource -f"
echo "Local test:    curl http://127.0.0.1:5050/api/search?q=test"
if [[ "$DOMAIN" != "_" ]]; then
  echo "After DNS points to this VM, enable HTTPS:"
  echo "sudo certbot --nginx -d $DOMAIN"
fi
