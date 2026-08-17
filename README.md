# Sangi — personal AI assistant over WhatsApp

A WhatsApp bot that pairs your number with an AI assistant (via [opencode](https://opencode.ai)). It handles messages, voice notes, images, scheduled tasks, bursts, and can generate images through Google Flow.

## Quick start (server)

Requires: **Node.js 20.6+**, **Python 3.10+**.

```bash
git clone https://github.com/HaseebUllahButt/Sangi.git
cd Sangi
./setup.sh
```

The script installs npm deps, creates a Python venv with Playwright for image gen, installs the opencode brain if missing, and generates `.env` for you.

Then edit `.env`:

| Variable | What it is |
|---|---|
| `GROQ_API_KEY` | Groq key for voice-note transcription (free at console.groq.com) |
| `ALLOWED_NUMBERS` | Your number(s), comma-separated, digits only — who can talk to the bot |
| `OPENCODE_URL` | Brain server URL (default `http://127.0.0.1:4096`) |
| `PAIR_PHONE_NUMBER` | Set to your number to get an 8-digit pairing code instead of a QR (handy headless) |

Start it:

```bash
npm start
```

On first run the bot prints a QR code (or pairing code) — scan it in WhatsApp → Linked Devices. That's it.

## What it does

- Chat AI replies in your language, personality in `AGENTS.md`
- Voice notes transcribed via Groq (`.ogg`)
- Image generation via Google Flow (`python3 image_gen/generate_image.py --prompt "..."`) — see [docs/FLOW_SETUP.md](docs/FLOW_SETUP.md) for the one-time account login
- Scheduled tasks: drop JSON into `task-queue/` (see `AGENTS.md`)
- Burst/multi-message sends, file delivery through `workspace/`

## Running as a service (optional)

```bash
sudo tee /etc/systemd/system/sangi.service >/dev/null <<'EOF'
[Unit]
Description=Sangi WhatsApp bot
After=network-online.target

[Service]
WorkingDirectory=/opt/Sangi
ExecStart=/usr/bin/node --env-file=.env index.js
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now sangi
```

The bot auto-starts the opencode brain (`npm run brain`) when needed, or run it yourself and set `OPENCODE_URL`.

## Running with Docker

No node/python on the host needed — everything (brain included) runs in one container:

```bash
git clone https://github.com/HaseebUllahButt/Sangi.git
cd Sangi
cp .env.example .env   # fill in GROQ_API_KEY / ALLOWED_NUMBERS

docker build -t sangi .
docker run -d --name sangi \
  --env-file .env \
  -v $PWD/creds:/app/creds \
  -v $PWD/workspace:/app/workspace \
  -v $PWD/downloads:/app/downloads \
  -v $PWD/task-queue:/app/task-queue \
  -v $PWD/outbox:/app/outbox \
  sangi
```

Check the QR:

```bash
docker logs -f sangi     # QR / pairing code appears here
```

Volumes keep your WhatsApp session and files after container restarts. Note: image generation is a separate service (needs a GUI for the one-time Flow login) and is not included in the container — run it on the host per [docs/FLOW_SETUP.md](docs/FLOW_SETUP.md).