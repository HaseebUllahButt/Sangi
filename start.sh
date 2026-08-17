#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p creds downloads workspace

if ! curl -sf http://127.0.0.1:4096/global/health >/dev/null; then
  echo "Starting opencode brain server..."
  nohup opencode serve --port 4096 --hostname 127.0.0.1 > /tmp/opencode-serve.log 2>&1 &
  sleep 3
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — open it and set GROQ_API_KEY and ALLOWED_NUMBERS."
fi

echo "Starting WhatsApp bot..."
exec node --env-file=.env index.js
