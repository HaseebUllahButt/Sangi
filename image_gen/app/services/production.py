"""Small, durable state surface for the driver workflow.

The media pipeline has several useful internal artifacts, but an operator
should only need one state file and one dashboard to understand a production.
This module owns that seam.  It deliberately stores summaries, not a second
copy of the media plan, so the existing prep/editor modules remain canonical.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable


STATE_FILENAME = "production.json"
DASHBOARD_FILENAME = "production.html"


def state_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "media" / STATE_FILENAME


def dashboard_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "media" / DASHBOARD_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_state(project_dir: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "project_dir": str(project_dir.resolve()),
        "phase": "planning",
        "message": "Starting production",
        "current": 0,
        "total": 0,
        "updated_at": _now(),
        "approvals": {"plan": False, "assets": False},
        "summary": {},
        "assets": [],
    }


def load(project_dir: str | Path) -> dict[str, Any]:
    path = state_path(project_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state(Path(project_dir))
    if not isinstance(data, dict):
        return _default_state(Path(project_dir))
    data.setdefault("version", 1)
    data.setdefault("approvals", {"plan": False, "assets": False})
    data.setdefault("assets", [])
    data.setdefault("summary", {})
    return data


def save(project_dir: str | Path, data: dict[str, Any]) -> Path:
    project = Path(project_dir)
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["project_dir"] = str(project.resolve())
    data["updated_at"] = _now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def update(
    project_dir: str | Path,
    *,
    phase: str | None = None,
    message: str | None = None,
    current: int | None = None,
    total: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    data = load(project_dir)
    if phase is not None:
        data["phase"] = phase
    if message is not None:
        data["message"] = message
    if current is not None:
        data["current"] = max(0, int(current))
    if total is not None:
        data["total"] = max(0, int(total))
    data.update(fields)
    save(project_dir, data)
    write_dashboard(project_dir, data)
    return data


def _relative_path(project_dir: Path, raw: str) -> str:
    if not raw:
        return ""
    try:
        return Path(raw).resolve().relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _asset_record(project_dir: Path, item: dict[str, Any]) -> dict[str, Any]:
    expected = str(item.get("ai_image_expected") or "")
    expected_stem = Path(expected).stem if expected else ""
    image_candidates = []
    if expected_stem:
        image_candidates = [
            f"media/images/{expected_stem}{suffix}"
            for suffix in (".jpg", ".jpeg", ".png", ".webp")
        ]
    path = _relative_path(project_dir, str(item.get("path") or ""))
    thumbnail = _relative_path(project_dir, str(item.get("thumbnail") or ""))
    return {
        "beat_id": str(item.get("beat_id") or ""),
        "text": str(item.get("text") or ""),
        "visual": str(item.get("visual") or ""),
        "t_start": item.get("t_start"),
        "t_end": item.get("t_end"),
        "status": str(item.get("status") or "planned"),
        "source": str(item.get("source") or ""),
        "score": float(item.get("score") or 0.0),
        "sync_required": bool(item.get("sync_required")),
        "sync_reason": str(item.get("sync_reason") or ""),
        "sync_type": str(item.get("sync_type") or ""),
        "trigger_quote": str(item.get("trigger_quote") or ""),
        "end_quote": str(item.get("end_quote") or ""),
        "sync_landing": str(item.get("sync_landing") or "cut_on_trigger"),
        "hide_before_trigger": bool(item.get("hide_before_trigger")),
        "required_visible": list(item.get("required_visible") or []),
        "sync_hold_until": item.get("sync_hold_until"),
        "importance": str(item.get("importance") or "support"),
        "expected": expected,
        "path": path,
        "thumbnail": thumbnail,
        "image_candidates": image_candidates,
    }


def _summary(assets: Iterable[dict[str, Any]]) -> dict[str, int]:
    rows = list(assets)
    covered = sum(1 for row in rows if row.get("status") == "covered")
    sync_total = sum(1 for row in rows if row.get("sync_required"))
    sync_covered = sum(
        1
        for row in rows
        if row.get("sync_required") and row.get("status") == "covered"
    )
    return {
        "beats": len(rows),
        "covered": covered,
        "needs_ai": sum(1 for row in rows if row.get("status") in {"needs_ai", "missing"}),
        "weak": sum(1 for row in rows if row.get("status") == "weak"),
        "sync_total": sync_total,
        "sync_covered": sync_covered,
        "ai_used": sum(1 for row in rows if row.get("source") == "ai"),
        "stock_used": sum(1 for row in rows if row.get("source") == "stock"),
        "library_used": sum(1 for row in rows if row.get("source") == "library"),
    }


def seed_plan(
    project_dir: str | Path,
    *,
    slug: str,
    plan: dict[str, Any],
    phase: str = "awaiting_plan_approval",
    message: str = "Beat plan ready for approval",
) -> dict[str, Any]:
    project = Path(project_dir)
    assets = []
    for index, beat in enumerate(plan.get("beats") or [], start=1):
        item = dict(beat)
        item.setdefault("beat_id", item.get("id") or f"beat-{index:03d}")
        item.setdefault("status", "planned")
        item.setdefault("ai_image_expected", "")
        assets.append(_asset_record(project, item))
    data = load(project)
    data.update(
        {
            "slug": slug,
            "phase": phase,
            "message": message,
            "current": 0,
            "total": len(assets),
            "audio_duration": plan.get("audio_duration"),
            "plan_source": plan.get("source", "manual"),
            "summary": _summary(assets),
            "assets": assets,
        }
    )
    data["approvals"] = {"plan": False, "assets": False}
    save(project, data)
    write_dashboard(project, data)
    return data


def refresh_from_media_plan(
    project_dir: str | Path,
    plan_path: str | Path | None = None,
    *,
    phase: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    project = Path(project_dir)
    path = Path(plan_path) if plan_path else project / "media" / "media_plan.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assets = [_asset_record(project, item) for item in payload.get("items") or []]
    data = load(project)
    data["slug"] = str(payload.get("slug") or data.get("slug") or project.name)
    data["audio_duration"] = payload.get("audio_duration")
    data["plan_source"] = payload.get("ai_mode") or data.get("plan_source", "")
    data["assets"] = assets
    data["summary"] = _summary(assets)
    data["current"] = sum(1 for row in assets if row.get("status") == "covered")
    data["total"] = len(assets)
    if phase is not None:
        data["phase"] = phase
    if message is not None:
        data["message"] = message
    save(project, data)
    write_dashboard(project, data)
    return data


def mark_asset_completed(
    project_dir: str | Path, beat_id: str, path: str | Path
) -> dict[str, Any] | None:
    project = Path(project_dir)
    state_file = state_path(project)
    if not state_file.is_file():
        return None
    data = load(project)
    for row in data.get("assets") or []:
        if row.get("beat_id") != beat_id:
            continue
        row["status"] = "covered"
        row["source"] = "ai"
        row["score"] = 1.0
        row["path"] = _relative_path(project, str(path))
        row["thumbnail"] = ""
        break
    data["phase"] = "gathering"
    data["current"] = sum(1 for row in data.get("assets") or [] if row.get("status") == "covered")
    data["summary"] = _summary(data.get("assets") or [])
    data["message"] = f"AI still saved for {beat_id}"
    save(project, data)
    write_dashboard(project, data)
    return data


def mark_asset_failed(project_dir: str | Path, beat_id: str, message: str) -> None:
    project = Path(project_dir)
    if not state_path(project).is_file():
        return
    data = load(project)
    for row in data.get("assets") or []:
        if row.get("beat_id") == beat_id:
            row["status"] = "needs_ai"
            break
    data["message"] = message
    data["summary"] = _summary(data.get("assets") or [])
    save(project, data)
    write_dashboard(project, data)


def _asset_image_candidates(row: dict[str, Any]) -> list[str]:
    candidates = list(row.get("image_candidates") or [])
    for key in ("thumbnail", "path"):
        value = str(row.get(key) or "")
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def write_dashboard(project_dir: str | Path, data: dict[str, Any] | None = None) -> Path:
    """Write a self-refreshing local visual board for planning and gathering."""
    project = Path(project_dir)
    data = data or load(project)
    rows = []
    for row in data.get("assets") or []:
        start = row.get("t_start")
        end = row.get("t_end")
        timing = ""
        if start is not None and end is not None:
            timing = f"{float(start):.2f}–{float(end):.2f}s"
        candidates = json.dumps(_asset_image_candidates(row), ensure_ascii=False)
        sync = " · sync-critical" if row.get("sync_required") else ""
        rows.append(
            "<article class='beat'>"
            f"<div class='thumb'><img data-candidates='{escape(candidates, quote=True)}' "
            f"alt='{escape(str(row.get('beat_id') or ''), quote=True)}' /></div>"
            "<div class='body'>"
            f"<div class='line'><strong>{escape(str(row.get('beat_id') or ''))}</strong> "
            f"<span class='badge status-{escape(str(row.get('status') or 'planned'))}'>"
            f"{escape(str(row.get('status') or 'planned'))}</span>"
            f"<span class='badge soft'>{escape(str(row.get('source') or 'planned'))}</span></div>"
            f"<div class='muted'>{escape(timing)}{escape(sync)}</div>"
            f"<div><strong>Visual:</strong> {escape(str(row.get('visual') or ''))}</div>"
            f"<div class='muted'><strong>VO:</strong> {escape(str(row.get('text') or '')[:240])}</div>"
            f"<div class='muted'>{escape(str(row.get('sync_reason') or ''))}</div>"
            + (
                f"<div class='muted'><strong>Land:</strong> "
                f"{escape(str(row.get('sync_type') or 'sync'))} on “"
                f"{escape(str(row.get('trigger_quote') or ''))}” · hold through “"
                f"{escape(str(row.get('end_quote') or ''))}”"
                f"{' · hide before reveal' if row.get('hide_before_trigger') else ''}</div>"
                f"<div class='muted'><strong>Must show:</strong> "
                f"{escape(', '.join(str(value) for value in (row.get('required_visible') or [])))}</div>"
                if row.get("sync_required")
                else ""
            )
            +
            "</div></article>"
        )
    summary = data.get("summary") or {}
    assembly = data.get("assembly_progress") or {}
    is_assembling = data.get("phase") == "assembling" and assembly
    total = int((assembly if is_assembling else data).get("total") or 0)
    current = int((assembly if is_assembling else data).get("current") or 0)
    percent = int(round(100 * current / total)) if total else 0
    progress_label = "render" if is_assembling else "assets"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta http-equiv="refresh" content="3" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Production — {escape(str(data.get('slug') or project.name))}</title>
<style>
body {{ margin: 0; padding: 24px; background: #0d1117; color: #e6edf3; font: 14px/1.45 system-ui, sans-serif; }}
header {{ position: sticky; top: 0; z-index: 2; background: #0d1117ee; padding-bottom: 18px; backdrop-filter: blur(10px); }}
h1 {{ margin: 0 0 4px; font-size: 22px; }}
.muted {{ color: #9da7b3; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0; }}
.stat, .badge {{ border-radius: 999px; padding: 3px 9px; background: #1b2633; color: #b9d7ff; font-size: 12px; }}
.progress {{ height: 8px; background: #202938; border-radius: 99px; overflow: hidden; max-width: 680px; }}
.progress > i {{ display: block; height: 100%; width: {percent}%; background: #45d483; }}
.grid {{ display: grid; gap: 10px; max-width: 1100px; }}
.beat {{ display: grid; grid-template-columns: 180px 1fr; gap: 14px; border: 1px solid #263241; border-radius: 10px; padding: 10px; background: #111923; }}
.thumb {{ min-height: 100px; background: #0a0e13; border-radius: 7px; display: grid; place-items: center; overflow: hidden; }}
.thumb img {{ width: 100%; max-height: 150px; object-fit: cover; }}
.line {{ margin-bottom: 5px; }}
.soft {{ background: #243044; color: #9ecbff; margin-left: 4px; }}
.status-covered {{ background: #173a2a; color: #7ddea0; }}
.status-needs_ai, .status-missing {{ background: #493512; color: #ffd166; }}
.status-weak {{ background: #433b18; color: #e8cd70; }}
.status-planned {{ background: #29313d; color: #c8d0da; }}
@media (max-width: 650px) {{ .beat {{ grid-template-columns: 1fr; }} .thumb {{ min-height: 150px; }} }}
</style></head><body>
<header><h1>Production — {escape(str(data.get('slug') or project.name))}</h1>
<div class="muted">{escape(str(data.get('phase') or 'planning'))} · {escape(str(data.get('message') or ''))}</div>
<div class="stats">
<span class="stat">{progress_label} {current}/{total}</span>
<span class="stat">sync {int(summary.get('sync_covered', 0))}/{int(summary.get('sync_total', 0))}</span>
<span class="stat">AI {int(summary.get('ai_used', 0))}</span>
<span class="stat">stock {int(summary.get('stock_used', 0))}</span>
<span class="stat">library {int(summary.get('library_used', 0))}</span>
</div><div class="progress"><i></i></div>
<p class="muted">Auto-refreshes every 3 seconds. Open this file while assets are being gathered.</p></header>
<main class="grid">{''.join(rows)}</main>
<script>
for (const image of document.querySelectorAll("img[data-candidates]")) {{
  let candidates = [];
  try {{ candidates = JSON.parse(image.dataset.candidates || "[]"); }} catch (_) {{}}
  let index = 0;
  const next = () => {{
    if (index >= candidates.length) {{ image.removeAttribute("src"); return; }}
    image.onerror = next;
    image.src = candidates[index++] + "?v=" + Date.now();
  }};
  next();
}}
</script></body></html>"""
    path = dashboard_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
