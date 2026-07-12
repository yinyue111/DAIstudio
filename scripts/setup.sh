#!/usr/bin/env bash
# One-time setup: python venv + deps, optional Playwright browser, DB init, npm ci.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> backend: venv + deps"
cd "$ROOT/backend"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  python - <<'PY'
from pathlib import Path
path = Path(".env")
text = path.read_text()
replacements = {
    "DEPLOY_ENV=production": "DEPLOY_ENV=local",
    "DEBUG=false": "DEBUG=true",
    "MOCK_MODE=false": "MOCK_MODE=true",
    "GATEWAY_BASE_URL=https://your-gateway": "GATEWAY_BASE_URL=",
    "GATEWAY_API_KEY=sk-xxxxxxxx": "GATEWAY_API_KEY=",
}
for old, new in replacements.items():
    text = text.replace(old, new)
path.write_text(text)
PY
  echo "   created backend/.env for local development (DEPLOY_ENV=local, DEBUG=true, MOCK_MODE=true)"
fi
if [ -f .env ]; then
  chmod 600 .env
fi

echo "==> (optional) Playwright chromium for dynamic-page scraping"
python -m playwright install chromium || echo "   skipped (fetcher falls back to httpx+bs4)"

echo "==> init DB + bootstrap admin (phone=${ADMIN_PHONE:-13800000000}, 1000 credits)"
# Password: init_db reads ADMIN_PASSWORD from the env; if unset it prints a
# one-time generated password below. No fixed default is used.
alembic upgrade head
python -m scripts.init_db --admin-phone "${ADMIN_PHONE:-13800000000}" --credits 1000
deactivate

echo "==> frontend: npm ci"
cd "$ROOT/frontend"
npm ci
[ -f .env.local ] || cp .env.local.example .env.local
if [ -f .env.local ]; then
  chmod 600 .env.local
fi

echo ""
echo "Setup complete. Start the three processes in separate terminals:"
echo "  ./scripts/run_backend.sh"
echo "  ./scripts/run_worker.sh"
echo "  ./scripts/run_beat.sh"
echo "  ./scripts/run_frontend.sh"
echo "Then open http://localhost:3000 and log in as 手机号 ${ADMIN_PHONE:-13800000000}."
echo "(管理员密码见上方 init_db 输出;若设置了 ADMIN_PASSWORD 环境变量则用该值。)"
