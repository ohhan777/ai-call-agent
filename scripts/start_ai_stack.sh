#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-5055}"

# stop previous local processes if any
pkill -f "uvicorn ai_call_server:app" >/dev/null 2>&1 || true
pkill -f "python ai_call_server.py" >/dev/null 2>&1 || true
pkill -f "ngrok http ${PORT}" >/dev/null 2>&1 || true

# start API server
nohup .venv/bin/python -m uvicorn ai_call_server:app --host 0.0.0.0 --port "$PORT" > output/ai_call_server.log 2>&1 &

# start ngrok
nohup ./scripts/ngrok_start.sh > output/ngrok.log 2>&1 &

sleep 3
PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | python3 -c 'import sys,json; d=json.load(sys.stdin); ts=d.get("tunnels",[]); print(ts[0]["public_url"] if ts else "")')

if [[ -z "$PUBLIC_URL" ]]; then
  echo "[오류] ngrok public URL을 가져오지 못했습니다. output/ngrok.log 확인하세요."
  exit 1
fi

ENV_FILE="$HOME/.openclaw/.env"
if grep -q '^TWILIO_PUBLIC_BASE_URL=' "$ENV_FILE"; then
  sed -i '' "s#^TWILIO_PUBLIC_BASE_URL=.*#TWILIO_PUBLIC_BASE_URL=\"${PUBLIC_URL}\"#" "$ENV_FILE"
else
  echo "TWILIO_PUBLIC_BASE_URL=\"${PUBLIC_URL}\"" >> "$ENV_FILE"
fi

echo "AI stack started"
echo "PORT: $PORT"
echo "PUBLIC_URL: $PUBLIC_URL"
echo "health: ${PUBLIC_URL}/health"
