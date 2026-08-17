#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "== Sangi setup =="

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "+ created .env from .env.example"
fi

if ! command -v node >/dev/null && [[ ! -x "$HOME/.local/share/mise/installs/node/26.5.0/bin/node" ]]; then
  echo "! node not found — install Node.js 20.6+ (e.g. via npm, nvm, or your distro) and re-run"
  exit 1
fi

node_version=$(node -v 2>/dev/null || "$HOME/.local/share/mise/installs/node/26.5.0/bin/node" -v)
echo "+ node: $node_version"

if node -e "process.exit(parseInt(process.versions.node.split('.')[0])<20?1:0)" 2>/dev/null; then
  echo "! node 20.6+ required (bot uses --env-file). Found $node_version"
  exit 1
fi

echo "+ installing npm dependencies..."
npm install --no-fund --no-audit

echo "+ setting up image generation (venv + playwright)..."
python3 -m venv image_gen/.venv
image_gen/.venv/bin/pip install -q -r image_gen/requirements.txt
image_gen/.venv/bin/playwright install chromium || echo "! chromium install failed — image gen needs it for Flow"

echo "+ checking brain server (opencode)..."
if ! command -v opencode >/dev/null; then
  echo "! opencode CLI not found."
  if command -v npm >/dev/null; then
    echo "  installing via npm:"
    npm install -g opencode-ai || echo "  npm install failed — install opencode manually from https://opencode.ai/install"
  else
    echo "  install it from https://opencode.ai/install and re-run"
    exit 1
  fi
fi

echo
echo "== Done. Next: =="
echo "1. Edit .env — set GROQ_API_KEY and ALLOWED_NUMBERS"
echo "2. Run: npm start   (bot prints a QR or 8-digit pairing code on first run)"
echo "3. Optional: check image gen — python3 image_gen/generate_image.py --help"