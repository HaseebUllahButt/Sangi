# Setting up the Google Flow account for image generation

Image generation runs through a real Chromium browser driving Google Flow
(labs.google/fx/tools/flow) — the profile that holds your Google login lives
in `image_gen/.flow/` (gitignored, never committed).

## Prerequisites

Already handled by `setup.sh` unless noted:

- Python 3.10+ venv with playwright: `image_gen/.venv`
- A browser: system Google Chrome is preferred (`google-chrome-stable`, falls
  back to system Chromium, then the playwright-bundled Chromium:
  `image_gen/.venv/bin/playwright install chromium`)
- Google account(s) declared in `.flow/profiles.json` as `enabled: true`

## One-time login (first run only)

Run from a **graphical session** (physical display, or VNC/X11-on-headless):

```bash
python3 image_gen/generate_image.py --prompt "test" --headed
```

A Chromium window opens. Sign in with your Google account (the one Flow is
linked to), approve, and let the prompt submit. The session is saved to the
profile dir and reused for all later runs.

### If your primary account is unavailable

Flow may report the account as blocked, out of credits, or in need of
recovery. The repo ships with a second verified account profile:

```bash
python3 image_gen/generate_image.py --prompt "test" --headed --account chromium-profile-3
```

`--account` accepts an enabled account name or directory from
`.flow/profiles.json`.

## Daily use

Headless, no login needed:

```bash
python3 image_gen/generate_image.py --prompt "a cat on a hoverboard"
```

The image lands in `workspace/` (pickup dir the bot auto-sends from) and the
script prints an `IMAGE_GENERATED:` line with the path.

## Accounts, parallelism, and health

- `.flow/profiles.json` lists accounts; flagged/blocked ones should stay
  `enabled: false` so they never run.
- Parallel workers: `--workers 5 --replicas 5` clones the profile per worker
  (Chrome can't share one user-data-dir). Never run two runners against the
  same project slice at once.
- The runner checks the Flow health tile and retries submission; treat
  repeated failures as a blocked account and switch with `--account`.

## Troubleshooting

- **`no enabled Flow account matches --profile-account`** — the name isn't an
  enabled account in `.flow/profiles.json`.
- **Login required again** — profile dir was deleted or the login expired;
  redo the one-time `--headed` login.
- **Browser rejects login / suspicious activity** — use the system Chrome
  channel (Google trusts it more), steady IP, and give the account a day
  before retrying.
- **No image produced** — run with `--headed` to watch the flow; the runner
  logs each stage under `image_gen/jobs/<job-id>/`.