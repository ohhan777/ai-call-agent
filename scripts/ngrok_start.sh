#!/usr/bin/env bash
set -e
TOKEN=$(grep '^NGROK_AUTH_TOKEN=' ~/.openclaw/.env | cut -d '=' -f2- || true)
TOKEN=${TOKEN%\"}
TOKEN=${TOKEN#\"}
if [ -n "$TOKEN" ]; then
  ngrok config add-authtoken "$TOKEN" >/dev/null 2>&1 || true
fi
PORT=${PORT:-5055}
ngrok http "$PORT"
