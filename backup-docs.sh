#!/usr/bin/env bash
set -euo pipefail

DRIVE=/mnt/Drive
SRC=/home/haseeb/whatsapp-agent/workspace
DEST="$DRIVE/whatsapp-docs/$(date +%F)"

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

if [[ ! -d "$DRIVE" ]]; then
  echo "[backup-docs] drive not mounted at $DRIVE" >&2
  exit 1
fi

shopt -s nullglob
files=("$SRC"/*)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "[backup-docs] nothing to move"
  exit 0
fi

if [[ $DRY -eq 1 ]]; then
  for f in "${files[@]}"; do
    echo "[dry-run] would move $f -> $DEST/$(basename "$f")"
  done
  exit 0
fi

mkdir -p "$DEST"
moved=0
for f in "${files[@]}"; do
  name=$(basename "$f")
  if [[ -e "$DEST/$name" ]]; then
    echo "[backup-docs] skip (already there): $name"
    continue
  fi
  mv "$f" "$DEST/$name"
  echo "[backup-docs] moved $name"
  moved=$((moved + 1))
done
echo "[backup-docs] done: $moved file(s) -> $DEST"
