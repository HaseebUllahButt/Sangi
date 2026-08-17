#!/usr/bin/env python3
"""Submit project-local image prompts to Google Flow through Chromium.

The first run is headed and requires a manual Google login. The browser profile
is then reused for later runs. This script drives the visible Flow UI only; it
does not call private endpoints, handle passwords, or bypass CAPTCHA/security
checks.

Install once:
    uv sync --extra flow
    # Only needed if no system Chrome/Chromium is installed:
    uv run playwright install chromium

First run:
    uv run python scripts/flow_runner.py --slug capone-empire

Parallel on one stable Gmail (clones the profile; Chrome cannot share one dir):
    uv run python scripts/flow_runner.py --slug capone-empire --workers 5 --replicas 5

Or set ``"replicas": 5`` on that account in ``.flow/profiles.json``.

Gmail profiles are declared in ``.flow/profiles.json`` (enabled accounts only).
Worker 1 is the first enabled account (costred366). Flagged accounts stay
``enabled: false``. Multiple workers on the same Gmail use profile *replicas*
under ``.flow/replicas/`` — never the same user-data-dir twice.

Startup bootstrap (headed + headless):
    dismiss delete/confirm dialogs → click "New project" (or open a project)
    → wait until the prompt box is visible → prefer Image mode.

Do not start a parallel run while another runner is processing the same
project: both processes would independently claim the same pending prompts.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import io
import json
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPT_DIR if (_SCRIPT_DIR / "app").is_dir() else _SCRIPT_DIR.parent
FLOW_DIR = ROOT / ".flow"
PROFILES_MANIFEST = FLOW_DIR / "profiles.json"
sys.path.insert(0, str(ROOT))

from app.utils.project_paths import resolve_project_dir  # noqa: E402
from app.services import production  # noqa: E402

FLOW_URL = "https://labs.google/fx/tools/flow"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
BLOCKED_MARKERS = (
    "unusual activity",
    "verify you are human",
    "captcha",
    "credits exhausted",
    "out of credits",
)
TRANSIENT_MARKERS = (
    "something went wrong",
    "try again",
    "temporarily unavailable",
    "high traffic",
    "generation failed",
    "couldn't create",
    "could not create",
)
# Flow image generations return media[].image.fifeUrl on this API route.
BATCH_IMAGE_ROUTE = "batchGenerateImages"
IMAGE_HOST_HINTS = (
    "googleusercontent.com",
    "googleapis.com",
    "ggpht.com",
    "labs.google",
)
MIN_IMAGE_BYTES = 150_000  # Flow blur placeholders are ~50-120KB
MIN_IMAGE_SIDE = 512
MIN_IMAGE_LONG_SIDE = 700
MAX_IMAGE_DOWNLOAD_BYTES = 50_000_000
IMAGE_DOWNLOAD_TIMEOUT_S = 120
# Flow keeps the composer submit disabled while it ingests an ingredient; a
# large WhatsApp photo routinely takes longer than the old 20s allowance.
CREATE_ENABLE_TIMEOUT_S = 90.0
REFERENCE_UPLOAD_TIMEOUT_S = 90.0
# The thumbnail Flow puts on the composer once a reference is a real ingredient.
INGREDIENT_CHIP_SELECTOR = 'img[alt*="present in your collection" i]'
# Flow flashes "Failed" on a tile and then retries the generation itself, so a
# failure is only believed once it has persisted this long.
FAILED_TILE_GRACE_S = 120.0

# Gospel: long-form = landscape 16:9, shorts = portrait 9:16.
# Set once per run via resolve_aspect(); image_quality_ok reads this default.
_ACTIVE_ASPECT = "portrait"


def resolve_aspect(project_dir: Path, explicit: str | None = None) -> str:
    """Pick landscape (long) or portrait (short). Explicit flag wins."""
    if explicit in {"landscape", "portrait"}:
        return explicit
    project_json = project_dir / "project.json"
    if project_json.is_file():
        try:
            data = json.loads(project_json.read_text(encoding="utf-8"))
            fmt = str(data.get("format") or "").lower()
            if fmt in {"long", "longform"}:
                return "landscape"
            if fmt in {"short", "shorts"}:
                return "portrait"
        except Exception:
            pass
    parts = {p.lower() for p in project_dir.parts}
    if "longforms" in parts or "longform" in parts:
        return "landscape"
    if "ytshorts" in parts or "shorts" in parts:
        return "portrait"
    return "portrait"


def set_active_aspect(aspect: str) -> str:
    global _ACTIVE_ASPECT
    _ACTIVE_ASPECT = "landscape" if aspect == "landscape" else "portrait"
    return _ACTIVE_ASPECT


def resolve_browser() -> dict[str, str]:
    """Pick a browser Google is least likely to reject for interactive login.

    Prefer real Google Chrome (channel=chrome). Fall back to system Chromium,
    then Playwright's bundled Chromium.
    """
    for name in ("google-chrome-stable", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return {"channel": "chrome", "label": f"chrome ({path})"}
    for name in ("chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return {"executable_path": path, "label": f"chromium ({path})"}
    return {"label": "playwright-chromium"}


async def ensure_window_onscreen(page) -> None:
    """Move/maximize the Chromium window so UI chrome is not clipped off-screen."""
    try:
        session = await page.context.new_cdp_session(page)
        info = await session.send("Browser.getWindowForTarget")
        window_id = info["windowId"]
        await session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"left": 0, "top": 0, "windowState": "normal"},
            },
        )
        await session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"windowState": "maximized"},
            },
        )
    except Exception:
        # Best-effort only; launch args still try to place the window.
        pass


def read_prompts(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"prompt file not found: {path}")
    prompts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            prompts.append(line)
    return prompts


def read_prompt_stems(manifest_path: Path, prompt_count: int) -> list[str]:
    """Labeled stems from prompts.manifest.json, else 01, 02, …"""
    defaults = [f"{i:02d}" for i in range(1, prompt_count + 1)]
    if not manifest_path.is_file():
        return defaults
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, list):
        return defaults
    stems: list[str] = []
    for i in range(prompt_count):
        stem = defaults[i]
        if i < len(data) and isinstance(data[i], dict):
            entry = data[i]
            raw = (
                entry.get("suggested_stem")
                or Path(str(entry.get("suggested_filename") or "")).stem
                or ""
            )
            raw = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(raw)).strip("-._")
            if raw:
                stem = raw
        stems.append(stem)
    return stems


def read_prompt_keys(manifest_path: Path, prompt_count: int) -> list[str]:
    """Stable per-prompt state keys — the beat id, never the prompt number.

    Prompt indexes are meaningless across packs: re-prep can turn 231 prompts
    into 60, and index 1 then names a different beat. Keying resume state on the
    index silently marks new prompts "done" and skips them.
    """
    keys = [f"idx:{i}" for i in range(1, prompt_count + 1)]
    if not manifest_path.is_file():
        return keys
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return keys
    if not isinstance(data, list):
        return keys
    for i in range(min(prompt_count, len(data))):
        entry = data[i]
        if isinstance(entry, dict) and entry.get("beat_id"):
            keys[i] = str(entry["beat_id"])
    return keys


def migrate_state_keys(
    state: dict[str, Any], keys: list[str], project_dir: Path
) -> int:
    """Re-key legacy index-keyed state onto beat ids, verifying against disk.

    An entry survives only when the file it claims to have written still exists
    and its name matches the beat that key now belongs to. Anything else is a
    stale mapping from a previous pack and is dropped, so the prompt re-runs.
    """
    completed = state.get("completed") or {}
    if not completed or not any(k.isdigit() for k in completed):
        return 0

    valid = set(keys)
    migrated: dict[str, Any] = {k: v for k, v in completed.items() if not k.isdigit()}
    dropped = 0
    for raw_key, rel_path in completed.items():
        if not raw_key.isdigit():
            continue
        name = Path(str(rel_path)).name
        # The written filename is prefixed with the beat it was generated for.
        match = next((k for k in valid if k.startswith("beat-") and name.startswith(k)), "")
        if not match or not (project_dir / str(rel_path)).is_file():
            dropped += 1
            continue
        migrated[match] = rel_path
    state["completed"] = migrated
    state["failed"] = {
        k: v for k, v in (state.get("failed") or {}).items() if not k.isdigit()
    }
    return dropped


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"completed": {}, "failed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"completed": {}, "failed": {}}
    data.setdefault("completed", {})
    data.setdefault("failed", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def exclusive_project_run(project_dir: Path):
    """Prevent separate runner processes from claiming the same prompt queue."""
    lock_path = project_dir / "media" / ".flow-run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another Flow runner is already active for {project_dir}; "
                "use --workers on that single process instead of launching a second one"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


@contextmanager
def exclusive_flow_browser():
    """Serialize all jobs that attach to the one persistent Flow browser."""
    lock_path = FLOW_DIR / ".global-run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    print("[flow] waiting for the shared browser lock...", flush=True)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        print("[flow] acquired the shared browser lock", flush=True)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


async def visible_text(page) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=2_000)).lower()
    except Exception:
        return ""


async def ensure_not_blocked(page) -> None:
    text = await visible_text(page)
    matches = [marker for marker in BLOCKED_MARKERS if marker in text]
    if matches:
        raise RuntimeError(
            "Flow requires manual attention (" + ", ".join(matches) + "). "
            "Resolve it in the visible browser, then rerun the command."
        )


async def ensure_generation_healthy(page) -> None:
    await ensure_not_blocked(page)
    text = await visible_text(page)
    matches = [marker for marker in TRANSIENT_MARKERS if marker in text]
    if matches:
        raise RuntimeError("Flow reported a transient error (" + ", ".join(matches) + ")")


async def all_tile_ids(page) -> set[str]:
    """IDs of every media tile Flow currently renders, generated or uploaded.

    Callers snapshot this before submitting so that anything appearing later
    belongs to their own run. Flow keeps earlier media — including failures —
    in the project, so identity is the only safe way to tell them apart.
    """
    found: set[str] = set()
    tiles = page.locator("[data-tile-id]")
    for index in range(await tiles.count()):
        tile = tiles.nth(index)
        try:
            if not await tile.is_visible():
                continue
            tile_id = await tile.get_attribute("data-tile-id")
            if tile_id:
                found.add(tile_id)
        except Exception:
            continue
    return found


async def failed_tile_ids(page) -> set[str]:
    """IDs of tiles Flow marked as failed (warning icon plus a 'Failed' label).

    Scoped to the tile itself. A page-wide text match would abort a healthy run
    because of a stale failure still sitting in the project's media list.
    """
    found: set[str] = set()
    tiles = page.locator('[data-tile-id]:has(i.google-symbols:text-is("warning"))')
    for index in range(await tiles.count()):
        tile = tiles.nth(index)
        try:
            if not await tile.is_visible():
                continue
            if not re.search(r"(?:^|\n)\s*failed\s*(?:$|\n)", await tile.inner_text(), re.I):
                continue
            tile_id = await tile.get_attribute("data-tile-id")
            if tile_id:
                found.add(tile_id)
        except Exception:
            continue
    return found


async def find_prompt_box(page):
    """Locate the Flow generation prompt input (image or generic).

    Prefer image-mode placeholders so we do not type into a hidden/offscreen
    field when multiple textareas exist.
    """
    selectors = (
        'textarea[placeholder*="image" i]',
        'textarea[placeholder*="Create an image" i]',
        'textarea[placeholder*="prompt" i]',
        'textarea[aria-label*="prompt" i]',
        'textarea[aria-label*="image" i]',
        '[contenteditable="true"][role="textbox"]',
        '[role="textbox"]',
        '[contenteditable="true"]',
        "textarea",
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


async def _click_visible_button(page, name_re: re.Pattern[str], *, exact: bool = False) -> bool:
    """Click the first visible, enabled button matching ``name_re``."""
    try:
        buttons = page.get_by_role("button", name=name_re, exact=exact)
        count = await buttons.count()
    except Exception:
        return False
    for index in range(count):
        button = buttons.nth(index)
        try:
            if await button.is_visible() and await button.is_enabled():
                await button.click(timeout=5_000)
                return True
        except Exception:
            continue
    return False


async def dismiss_flow_dialogs(page) -> int:
    """Close confirm/modals that block the project list or editor.

    Debug captures showed a sticky "Are you sure you want to delete this
    project?" dialog on the home screen — that fully covers the UI and makes
    the prompt box unfindable. Always Cancel; never click Delete.
    """
    dismissed = 0

    # Prefer Cancel inside an open dialog.
    try:
        dialogs = page.locator('[role="dialog"], [data-state="open"]')
        n = await dialogs.count()
    except Exception:
        n = 0
    for i in range(n):
        dialog = dialogs.nth(i)
        try:
            if not await dialog.is_visible():
                continue
        except Exception:
            continue
        text = ""
        try:
            text = (await dialog.inner_text(timeout=500) or "").lower()
        except Exception:
            pass
        # Destructive confirm → Cancel only.
        if "delete" in text or "permanently" in text or "are you sure" in text:
            for label in (r"^Cancel$", r"^Close$", r"^No$", r"^Dismiss$"):
                try:
                    btn = dialog.get_by_role("button", name=re.compile(label, re.I))
                    if await btn.count() and await btn.first.is_visible():
                        await btn.first.click(timeout=3_000)
                        dismissed += 1
                        await page.wait_for_timeout(300)
                        break
                except Exception:
                    continue
            continue
        # Generic open dialog: try Cancel / Close / Got it / OK (not Delete).
        for label in (r"^Cancel$", r"^Close$", r"^Got it$", r"^Dismiss$", r"^OK$"):
            try:
                btn = dialog.get_by_role("button", name=re.compile(label, re.I))
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.click(timeout=3_000)
                    dismissed += 1
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                continue

    # Global Cancel if a confirm still peeks through without role=dialog.
    if await _click_visible_button(page, re.compile(r"^Cancel$", re.I)):
        dismissed += 1
        await page.wait_for_timeout(250)

    # Escape clears menus / half-open popovers without confirming deletes.
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except Exception:
        pass

    if dismissed:
        print(f"[flow] dismissed {dismissed} blocking dialog(s)", flush=True)
    return dismissed


async def click_new_project(page) -> bool:
    """Click Flow home 'New project' (add_2 icon + label)."""
    patterns = (
        re.compile(r"^\s*New project\s*$", re.I),
        re.compile(r"New project", re.I),
        re.compile(r"Create project", re.I),
        re.compile(r"^\s*\+\s*New", re.I),
    )
    for pattern in patterns:
        if await _click_visible_button(page, pattern):
            print("[flow] clicked New project", flush=True)
            await page.wait_for_timeout(1500)
            return True
    # Icon-only / overlay button: match text content via locator.
    try:
        loc = page.locator("button", has_text=re.compile(r"New project", re.I))
        for index in range(await loc.count()):
            candidate = loc.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click(timeout=5_000)
                print("[flow] clicked New project (text locator)", flush=True)
                await page.wait_for_timeout(1500)
                return True
    except Exception:
        pass
    return False


async def ensure_image_generation_mode(page) -> None:
    """Prefer Image / Create Image mode so the still prompt box is active."""
    # Current Flow project editors expose the image composer directly as a
    # contenteditable prompt with a model selector.  Clicking the older
    # "Create Image" menu item in this state only reopens the menu, so avoid
    # repeatedly toggling it when we are already inside a project.
    if "/project/" in page.url:
        box = await find_prompt_box(page)
        if box is not None:
            return

    # If the visible prompt already mentions image, leave it alone.
    box = await find_prompt_box(page)
    if box is not None:
        try:
            placeholder = (await box.get_attribute("placeholder") or "").lower()
            aria = (await box.get_attribute("aria-label") or "").lower()
            if "image" in placeholder or "image" in aria:
                return
        except Exception:
            pass

    mode_patterns = (
        re.compile(r"^\s*Image\s*$", re.I),
        re.compile(r"Create Image", re.I),
        re.compile(r"Text to Image", re.I),
        re.compile(r"Images?", re.I),
    )
    for pattern in mode_patterns:
        if await _click_visible_button(page, pattern):
            print(f"[flow] switched generation mode via {pattern.pattern}", flush=True)
            await page.wait_for_timeout(800)
            return


async def dismiss_drop_overlay(page) -> None:
    """Release Flow's full-screen media drop layer when a prior run left it open."""
    drop = page.get_by_text("Drop media", exact=True)
    if not await drop.count():
        return
    try:
        await drop.first.evaluate(
            """el => {
                const transfer = new DataTransfer();
                for (const type of [\"dragleave\", \"dragend\"]) {
                    el.dispatchEvent(new DragEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        dataTransfer: transfer,
                    }));
                }
            }"""
        )
        await page.wait_for_timeout(300)
    except Exception:
        pass


async def ensure_flow_editor_ready(page, *, timeout_s: float = 60.0) -> None:
    """Get from Flow home (project list / dialogs) into an editor with a prompt box.

    Headed runs historically relied on the operator to Cancel popups and click
    New project before pressing Enter. Headless never did that — so workers
    failed with "Could not find the Flow prompt box". This path automates it.
    """
    await dismiss_drop_overlay(page)
    await ensure_not_blocked(page)
    await dismiss_flow_dialogs(page)

    # A fresh browser starts at Flow's public landing page.  The editor CTA
    # performs the Google OAuth handoff; once authenticated it returns to the
    # project list.  Do this before looking for generic textareas on the
    # landing page, which are not the generation composer.
    if "/project/" not in page.url:
        for pattern in (
            re.compile(r"^Create with Google Flow$", re.I),
            re.compile(r"^Try in Google Flow$", re.I),
        ):
            if await _click_visible_button(page, pattern, exact=True):
                await page.wait_for_timeout(2500)
                break
        if "accounts.google.com" in page.url:
            raise RuntimeError(
                "Google login is required for this Flow profile; complete it in a headed run first."
            )

    if await find_prompt_box(page) is not None:
        await ensure_image_generation_mode(page)
        if await find_prompt_box(page) is not None:
            return

    # Still on the project list / empty home → open a fresh project.
    print("[flow] prompt box not ready; opening a Flow project…", flush=True)
    opened = await click_new_project(page)
    if not opened:
        # Fallback: open the first existing project card if any.
        try:
            # Prefer explicit project tiles over random buttons — look for edit/open.
            for sel in (
                page.get_by_role("button", name=re.compile(r"Edit project|Open project", re.I)),
                page.locator("a[href*='/project']"),
                page.locator("a[href*='flow']").filter(has_text=re.compile(r".+", re.I)),
            ):
                try:
                    if await sel.count() and await sel.first.is_visible():
                        await sel.first.click(timeout=5_000)
                        print("[flow] opened existing project card", flush=True)
                        opened = True
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if not opened:
        # One more dismiss + New project attempt after Escape cascade.
        await dismiss_flow_dialogs(page)
        opened = await click_new_project(page)

    deadline = time.monotonic() + max(5.0, timeout_s)
    retried_new = False
    while time.monotonic() < deadline:
        await ensure_not_blocked(page)
        await dismiss_flow_dialogs(page)
        await ensure_image_generation_mode(page)
        if await find_prompt_box(page) is not None:
            print("[flow] editor ready (prompt box visible)", flush=True)
            return
        # Sometimes New project is slow; one mid-wait re-click only.
        if not retried_new and time.monotonic() > deadline - (timeout_s * 0.45):
            retried_new = True
            await click_new_project(page)
        await page.wait_for_timeout(800)

    raise RuntimeError(
        "Could not reach a Flow editor with a prompt box. "
        "Dismiss any dialogs, click New project (or open a project), "
        "confirm Image mode is selected, then rerun. "
        "Debug: media/flow-debug/ screenshots."
    )


async def fill_prompt(page, prompt: str) -> None:
    await ensure_flow_editor_ready(page, timeout_s=45.0)
    box = await find_prompt_box(page)
    if box is None:
        raise RuntimeError(
            "Could not find the Flow prompt box. The UI may have changed; "
            "inspect the saved debug screenshot and update the selector."
        )
    await box.click()
    tag = await box.evaluate("el => el.tagName.toLowerCase()")
    if tag == "textarea" or tag == "input":
        await box.fill(prompt)
    else:
        await box.press("Control+A")
        await page.keyboard.insert_text(prompt)


async def _ingredient_file_input(page):
    """Return Flow's hidden file input for prompt ingredients, if open."""
    inputs = page.locator('input[type="file"]')
    for index in range(await inputs.count()):
        candidate = inputs.nth(index)
        try:
            parent_text = await candidate.locator("xpath=..").inner_text(timeout=500)
        except Exception:
            continue
        if "add ingredients" in parent_text.lower():
            return candidate
    return None


async def _dispatch_file_drag(page, box) -> None:
    """Open Flow's ingredient drop surface without uploading through JS."""
    await box.evaluate(
        """el => {
            const transfer = new DataTransfer();
            transfer.items.add(new File([\"reference\"], \"reference.png\", {
                type: \"image/png\"
            }));
            for (const type of [\"dragenter\", \"dragover\"]) {
                el.dispatchEvent(new DragEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    dataTransfer: transfer,
                }));
            }
        }"""
    )


async def attach_reference_images(page, references: list[str]) -> None:
    """Attach local image files to Flow's prompt as visual ingredients."""
    paths = [Path(raw).expanduser().resolve() for raw in references if raw]
    if not paths:
        return
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("reference image not found: " + ", ".join(missing))
    invalid = [str(path) for path in paths if path.suffix.lower() not in IMAGE_EXTENSIONS]
    if invalid:
        raise ValueError("reference files must be images: " + ", ".join(invalid))

    box = await find_prompt_box(page)
    if box is None:
        raise RuntimeError("Could not find Flow prompt box while attaching reference images")
    ingredient_input = await _ingredient_file_input(page)
    if ingredient_input is None:
        await _dispatch_file_drag(page, box)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ingredient_input = await _ingredient_file_input(page)
            if ingredient_input is not None:
                break
            await page.wait_for_timeout(250)
    if ingredient_input is None:
        raise RuntimeError("Flow did not open the Add Ingredients drop surface")

    print(
        "[flow] attaching reference image(s): "
        + ", ".join(path.name for path in paths),
        flush=True,
    )
    await ingredient_input.set_input_files([str(path) for path in paths])
    await page.wait_for_timeout(1_000)

    # Our synthetic dragenter opened a full-screen drop layer, and Flow only
    # moves the upload onto the composer once that drag state ends. Release it
    # before waiting for anything, or the composer never reappears at all.
    await dismiss_drop_overlay(page)

    # The upload is asynchronous and large WhatsApp photos are slow to ingest.
    # Flow flashes a failed state on the media tile while it retries internally,
    # so that tile says nothing useful here. The composer chip is the signal that
    # the ingredient was actually accepted; without it the run would silently
    # generate from the prompt alone and quietly ignore the reference.
    chips = page.locator(INGREDIENT_CHIP_SELECTOR)
    deadline = time.monotonic() + REFERENCE_UPLOAD_TIMEOUT_S
    while True:
        try:
            attached = await chips.count()
        except Exception:
            attached = 0
        if attached >= len(paths):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Flow attached {attached}/{len(paths)} reference image(s) within "
                f"{REFERENCE_UPLOAD_TIMEOUT_S:.0f}s"
            )
        await dismiss_drop_overlay(page)
        await page.wait_for_timeout(500)
    print(f"[flow] reference ingredient(s) attached: {len(paths)}", flush=True)
    # Flow keeps the composer submit disabled until ingest finishes; click_generate
    # waits that out, which is what keeps a half-uploaded ingredient from being
    # dropped from the generation.


async def click_generate(page, baseline_tile_ids: set[str] | None = None) -> None:
    """Submit the composer, then confirm Flow actually started a generation.

    ``baseline_tile_ids`` is the gallery snapshot taken just before this call;
    any tile appearing after it belongs to this submission.
    """
    baseline = baseline_tile_ids or set()
    # Current Flow has two buttons named Create. The composer submit control is
    # the arrow-forward button; the other one opens the media creation dialog.
    button = page.locator("button:has(i.google-symbols:text-is('arrow_forward'))").last

    async def submit_ready() -> bool:
        try:
            return bool(
                await button.count()
                and await button.is_visible()
                and await button.is_enabled()
            )
        except Exception:
            return False

    # Flow disables this control while it ingests an ingredient and while one of
    # its own jobs is running, so a disabled button is not a missing one.
    deadline = time.monotonic() + CREATE_ENABLE_TIMEOUT_S
    while not await submit_ready():
        if time.monotonic() >= deadline:
            names = re.compile(r"^(generate|create|send|submit|run|make)$", re.I)
            buttons = page.get_by_role("button", name=names)
            button = None
            for index in range(await buttons.count() - 1, -1, -1):
                candidate = buttons.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    button = candidate
                    break
            if button is not None:
                break
            if (await all_tile_ids(page)) - baseline:
                print("[flow] generation already in progress; waiting for its tile", flush=True)
                return
            raise RuntimeError("Could not find an enabled Flow generate control")
        await page.wait_for_timeout(250)

    for click_attempt in range(1, 3):
        if click_attempt == 1:
            await button.click(timeout=5_000)
        else:
            print("[flow] Create click was not acknowledged; retrying once", flush=True)
            await button.evaluate("el => el.click()")

        confirm_deadline = time.monotonic() + 10.0
        while time.monotonic() < confirm_deadline:
            await ensure_generation_healthy(page)
            if (await all_tile_ids(page)) - baseline:
                print("[flow] Create submission confirmed by new gallery tile", flush=True)
                return
            if not await submit_ready():
                print("[flow] Create submission confirmed by disabled control", flush=True)
                return
            await page.wait_for_timeout(250)

    raise RuntimeError("Flow Create click did not start generation")


# Flow gen-settings: older UI used role=tab "1x"/"x2"; current UI embeds count
# on the model chip: "🍌 Nano Banana 2 Lite [crop_16_9] 1x".
_COUNT_TAB_TEXT_RE = re.compile(r"^(1x|x[2-4])$")
_COUNT_IN_TEXT_RE = re.compile(r"(?:^|[\s\W])(?:x?\s*([1-4])\s*x|([1-4])\s*x)(?:$|[\s\W])", re.I)
_ASPECT_ICONS = {
    "landscape": ("crop_16_9", "crop_landscape"),
    "portrait": ("crop_9_16", "crop_portrait"),
}
_GEN_SETTINGS_BUTTON_SELECTORS = (
    "button:has(i.google-symbols:text('crop_16_9'))",
    "button:has(i.google-symbols:text('crop_9_16'))",
    "button:has(i.google-symbols:text('crop_square'))",
    "button:has(i.google-symbols:text('crop_portrait'))",
    "button:has(i.google-symbols:text('crop_landscape'))",
    "button:has(i.google-symbols:text-is('tune'))",
    "button[aria-label*='settings' i]",
    "button[aria-label*='ratio' i]",
    "button[aria-label*='model' i]",
)


def _parse_count_from_text(text: str) -> int | None:
    raw = (text or "").strip()
    if not raw:
        return None
    # Normalize zero-width / emoji noise so glued chip text parses cleanly.
    raw = re.sub(r"[\u200b-\u200d\ufeff]", "", raw)
    # Current Flow (2026): chip text is often
    #   "🍌 Nano Banana 2 Litecrop_16_9x1"  or  "...Lite 1x"
    # i.e. count is "x1" glued after the crop icon name, not "1x".
    after_crop = re.search(r"crop_\d+_\d+\s*[x×]\s*([1-4])\b", raw, re.I)
    if after_crop:
        return int(after_crop.group(1))
    after_crop_1x = re.search(r"crop_\d+_\d+\s*([1-4])\s*[x×]\b", raw, re.I)
    if after_crop_1x:
        return int(after_crop_1x.group(1))
    # Older glued forms: "...Lite1x" / "...banana2x"
    glued = re.search(
        r"(?:crop_\d+_\d+|lite|banana|imagen|ultra)\s*([1-4])\s*[x×]\b",
        raw,
        re.I,
    )
    if glued:
        return int(glued.group(1))
    # Prefer trailing "1x" / "x2" / "×1" style tokens (with or without space).
    for match in re.finditer(
        r"(?:^|[\s\W_])(?:[x×]\s*([1-4])|([1-4])\s*[x×])(?:$|[\s\W])",
        raw,
        re.I,
    ):
        digit = match.group(1) or match.group(2)
        if digit:
            return int(digit)
    # Last-resort: ends with 1x / x1 / ×1.
    tail = re.search(r"(?:[x×]\s*([1-4])|([1-4])\s*[x×])\s*$", raw, re.I)
    if tail:
        return int(tail.group(1) or tail.group(2))
    if re.fullmatch(r"[1-4]", raw):
        return int(raw)
    return None


def _count_tabs(page):
    return page.locator('[role="tab"]').filter(has_text=_COUNT_TAB_TEXT_RE)


async def _visible_count_tabs(page):
    """Return the visible count-tab group (legacy Flow UI)."""
    tabs = _count_tabs(page)
    return [tabs.nth(index) for index in range(await tabs.count()) if await tabs.nth(index).is_visible()]


async def _model_settings_chip(page):
    """Current Flow: single chip with model name + crop icon + 1x count."""
    for selector in (
        "button:has(i.google-symbols:text('crop_16_9'))",
        "button:has(i.google-symbols:text('crop_9_16'))",
        "button:has(i.google-symbols:text('crop_square'))",
        "button:has(i.google-symbols:text('crop_landscape'))",
        "button:has(i.google-symbols:text('crop_portrait'))",
    ):
        loc = page.locator(selector)
        for index in range(await loc.count()):
            button = loc.nth(index)
            try:
                if not await button.is_visible():
                    continue
                text = (await button.text_content(timeout=500) or "")
                if _parse_count_from_text(text) is not None or "banana" in text.lower():
                    return button
                # Crop-only chip still opens the settings popover.
                return button
            except Exception:
                continue
    # Fallback: button whose label ends with 1x / 2x.
    try:
        buttons = page.locator("button")
        for index in range(await buttons.count() - 1, -1, -1):
            button = buttons.nth(index)
            if not await button.is_visible():
                continue
            text = (await button.text_content(timeout=300) or "").strip()
            if _parse_count_from_text(text) is not None and len(text) < 80:
                return button
    except Exception:
        pass
    return None


async def _read_output_count(page) -> int | None:
    """Best-effort read of outputs-per-prompt (1–4)."""
    try:
        # Legacy tab UI.
        for tab in await _visible_count_tabs(page):
            if await tab.get_attribute("aria-selected") == "true":
                text = (await tab.text_content(timeout=500) or "").strip()
                parsed = _parse_count_from_text(text)
                if parsed is not None:
                    return parsed
        # Current chip UI: "… crop_16_9 1x"
        chip = await _model_settings_chip(page)
        if chip is not None:
            text = (await chip.text_content(timeout=500) or "")
            parsed = _parse_count_from_text(text)
            if parsed is not None:
                return parsed
        # Any visible control labelled 1x–4x.
        for label in ("1x", "2x", "3x", "4x", "x1", "x2", "x3", "x4"):
            loc = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
            if await loc.count() and await loc.first.is_visible():
                if await loc.first.get_attribute("aria-pressed") == "true" or (
                    await loc.first.get_attribute("aria-selected") == "true"
                ):
                    return _parse_count_from_text(label)
        return None
    except Exception:
        return None


async def _settings_panel_open(page) -> bool:
    """True when count picker (tabs or buttons) is visible."""
    try:
        if await _visible_count_tabs(page):
            return True
        # Popover options after opening the model chip.
        for label in ("1x", "2x", "3x", "4x", "x1", "x2", "x3", "x4"):
            loc = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
            if await loc.count() and await loc.first.is_visible():
                return True
            loc = page.get_by_role("menuitem", name=re.compile(rf"^{label}$", re.I))
            if await loc.count() and await loc.first.is_visible():
                return True
            loc = page.get_by_role("option", name=re.compile(rf"^{label}$", re.I))
            if await loc.count() and await loc.first.is_visible():
                return True
        # Aspect options in the same popover.
        for icon in ("crop_16_9", "crop_9_16", "crop_square"):
            if await page.locator(f"[role='menu'] i.google-symbols:text('{icon}')").count():
                return True
            if await page.locator(f"[role='dialog'] i.google-symbols:text('{icon}')").count():
                return True
        return False
    except Exception:
        return False


async def _open_gen_settings(page) -> bool:
    if await _settings_panel_open(page):
        return True
    # Prefer the model/settings chip next to the prompt (current Flow UI).
    chip = await _model_settings_chip(page)
    if chip is not None:
        try:
            await chip.click(timeout=2000)
            await page.wait_for_timeout(500)
            if await _settings_panel_open(page):
                return True
            # Chip already displays settings; some builds don't open a panel.
            # Treat chip presence as enough to proceed with aspect/count checks.
            return True
        except Exception:
            pass
    for selector in _GEN_SETTINGS_BUTTON_SELECTORS:
        try:
            button = page.locator(selector).first
            if not await button.is_visible():
                continue
            await button.click(timeout=2000)
            await page.wait_for_timeout(500)
            if await _settings_panel_open(page):
                return True
        except Exception:
            continue
    try:
        candidates = page.locator("button").filter(
            has=page.locator("i.google-symbols")
        )
        for index in range(min(await candidates.count(), 12)):
            button = candidates.nth(index)
            if not await button.is_visible():
                continue
            await button.click(timeout=1500)
            await page.wait_for_timeout(400)
            if await _settings_panel_open(page):
                return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    except Exception:
        pass
    return False


async def ensure_aspect_ratio(page, aspect: str) -> None:
    """Click Flow's crop control for landscape 16:9 or portrait 9:16."""
    want = "landscape" if aspect == "landscape" else "portrait"
    icons = _ASPECT_ICONS[want]

    async def _click_icon(icon: str) -> bool:
        selectors = (
            f"button:has(i.google-symbols:text('{icon}'))",
            f"[role='menuitem']:has(i.google-symbols:text('{icon}'))",
            f"[role='option']:has(i.google-symbols:text('{icon}'))",
            f"i.google-symbols:text('{icon}')",
        )
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if not await loc.is_visible():
                    continue
                await loc.click(timeout=2000)
                await page.wait_for_timeout(300)
                print(f"[flow] aspect set via {icon} ({want})", flush=True)
                return True
            except Exception:
                continue
        return False

    # If chip already shows the right crop icon, done.
    chip = await _model_settings_chip(page)
    if chip is not None:
        try:
            chip_html = await chip.inner_html(timeout=500)
            if any(icon in chip_html for icon in icons):
                print(f"[flow] aspect already {want} on model chip", flush=True)
                return
        except Exception:
            pass

    for icon in icons:
        if await _click_icon(icon):
            return

    # Open chip popover then choose aspect.
    if chip is not None:
        try:
            await chip.click(timeout=2000)
            await page.wait_for_timeout(400)
            for icon in icons:
                if await _click_icon(icon):
                    return
        except Exception:
            pass

    labels = (
        re.compile(r"16\s*:\s*9|landscape|widescreen", re.I)
        if want == "landscape"
        else re.compile(r"9\s*:\s*16|portrait|vertical", re.I)
    )
    try:
        loc = page.get_by_role("button", name=labels)
        if await loc.count() and await loc.first.is_visible():
            await loc.first.click(timeout=2000)
            await page.wait_for_timeout(300)
            print(f"[flow] aspect set via label ({want})", flush=True)
            return
    except Exception:
        pass
    print(
        f"[flow] warning: could not click {want} aspect control; "
        "set crop_16_9 / crop_9_16 manually if needed",
        flush=True,
    )


async def _click_count_option(page, count: int = 1) -> bool:
    """Select Nx outputs in whatever UI Flow is showing."""
    labels = [f"{count}x", f"x{count}", str(count)]
    for role in ("tab", "button", "menuitem", "option", "radio"):
        for label in labels:
            try:
                loc = page.get_by_role(role, name=re.compile(rf"^{re.escape(label)}$", re.I))
                for index in range(await loc.count()):
                    item = loc.nth(index)
                    if await item.is_visible():
                        await item.click(timeout=2000)
                        await page.wait_for_timeout(300)
                        return True
            except Exception:
                continue
    # Text match fallback.
    for label in labels:
        try:
            loc = page.locator(f"text=/{re.escape(label)}/i").first
            if await loc.is_visible():
                await loc.click(timeout=2000)
                await page.wait_for_timeout(300)
                return True
        except Exception:
            continue
    return False


async def ensure_single_image_output(page, *, aspect: str = "portrait") -> None:
    """Force Flow outputs to 1x and correct aspect before generate.

    Flow remembers x2/x3/x4 and crop ratio across runs; without this, one
    prompt can mint a grid or the wrong orientation.

    Current UI (2026): model chip shows ``Banana … crop_16_9 1x`` — there are
    no role=tab count controls until/unless a popover opens.
    """
    print(f"[flow] setting outputs to 1 image, aspect={aspect}...", flush=True)

    # Fast path: chip already says 1x — still enforce aspect, don't hard-fail.
    current = await _read_output_count(page)
    if current == 1:
        print("[flow] outputs already 1x (model chip)", flush=True)
        await ensure_aspect_ratio(page, aspect)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        return

    opened = await _open_gen_settings(page)
    if not opened:
        # Chip may still be readable without a popover.
        current = await _read_output_count(page)
        if current == 1:
            print("[flow] outputs already 1x (no settings panel)", flush=True)
            await ensure_aspect_ratio(page, aspect)
            return
        print(
            "[flow] warning: could not open gen settings panel; "
            "continuing if chip already looks correct",
            flush=True,
        )

    await ensure_aspect_ratio(page, aspect)

    current = await _read_output_count(page)
    if current == 1:
        print("[flow] outputs already 1x", flush=True)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        return

    # Need to switch count → open chip again if panel closed after aspect click.
    if not await _settings_panel_open(page):
        chip = await _model_settings_chip(page)
        if chip is not None:
            try:
                await chip.click(timeout=2000)
                await page.wait_for_timeout(400)
            except Exception:
                pass

    clicked = await _click_count_option(page, 1)
    if clicked:
        print("[flow] clicked 1x option", flush=True)
    else:
        # Legacy tabs.
        try:
            tabs = await _visible_count_tabs(page)
            for candidate in tabs:
                if (await candidate.text_content() or "").strip().lower() in {"1x", "x1", "1"}:
                    await candidate.click(timeout=3000)
                    await page.wait_for_timeout(300)
                    clicked = True
                    break
        except Exception as exc:
            print(f"[flow] warning: could not click 1x control: {exc}", flush=True)

    final = await _read_output_count(page)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)

    if final == 1:
        print("[flow] outputs set to 1x", flush=True)
        return

    # Soft continue when UI is already showing 1x on the closed chip but we
    # could not re-read after Escape, or panel never exposed options.
    chip = await _model_settings_chip(page)
    if chip is not None:
        try:
            text = (await chip.text_content(timeout=500) or "")
            html = await chip.inner_html(timeout=500)
            blob = f"{text} {html}"
            if _parse_count_from_text(text) == 1 or _parse_count_from_text(blob) == 1:
                print("[flow] outputs appear 1x on model chip; continuing", flush=True)
                return
            # Chip shows crop icon + "x1" as sibling text node after <i>.
            if re.search(r">\s*[x×]\s*1\s*<|[x×]\s*1\s*$", html, re.I) or re.search(
                r"[x×]\s*1\b", text, re.I
            ):
                print("[flow] outputs appear x1 in chip HTML; continuing", flush=True)
                return
        except Exception:
            pass

    # If we already clicked 1x / chip is landscape and unreadable, don't hard-fail —
    # Flow UI changes often break the counter scrape while settings are correct.
    if final is None and (clicked or current is None):
        print(
            "[flow] warning: could not re-verify 1x after click "
            f"(read={final!r}, clicked={clicked}); continuing — keep Flow on 1x",
            flush=True,
        )
        return

    if final is None and not clicked:
        print(
            "[flow] warning: could not verify 1x output count "
            f"(read={final!r}); continuing — confirm Flow is on 1x",
            flush=True,
        )
        return

    raise RuntimeError(
        f"Flow output count is {final!r}, not 1x; refusing to spend credits. "
        "Open the model chip next to the prompt and pick 1x, then retry."
    )


def _walk_fife_urls(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"fifeUrl", "fife_url", "url", "imageUrl", "downloadUrl"} and isinstance(
                value, str
            ):
                if value.startswith("https://") and any(h in value for h in IMAGE_HOST_HINTS):
                    found.append(value)
            else:
                _walk_fife_urls(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_fife_urls(item, found)


class ImageCapture:
    """Collect generated image URLs from Flow network traffic.

    Prefer API ``fifeUrl`` values. Ignore most raw ``image/*`` responses — Flow
    serves grayscale blur placeholders there while generation is still running.
    """

    def __init__(self) -> None:
        self.api_urls: list[str] = []
        self.image_urls: list[str] = []
        self.seen: set[str] = set()
        self.rejected: set[str] = set()
        self.events = 0
        self._lock = asyncio.Lock()

    @property
    def urls(self) -> list[str]:
        # API URLs first — those are the final assets.
        return [*self.api_urls, *self.image_urls]

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def detach(self, page) -> None:
        try:
            page.remove_listener("response", self._on_response)
        except Exception:
            pass

    def reset(self) -> None:
        self.api_urls.clear()
        self.image_urls.clear()
        self.seen.clear()
        self.rejected.clear()
        self.events = 0

    def mark_rejected(self, url: str) -> None:
        self.rejected.add(url)
        self.api_urls = [u for u in self.api_urls if u != url]
        self.image_urls = [u for u in self.image_urls if u != url]

    def next_url(self) -> str | None:
        for url in self.urls:
            if url not in self.rejected:
                return url
        return None

    async def _on_response(self, response) -> None:
        try:
            url = response.url or ""
            status = response.status
            if status != 200:
                return
            ctype = (response.headers or {}).get("content-type", "").lower()

            if (
                BATCH_IMAGE_ROUTE in url
                or "flowMedia" in url
                or "aisandbox" in url
                or "generateimage" in url.lower()
                or "generateImage" in url
            ):
                if ctype and "json" not in ctype and "text" not in ctype and "javascript" not in ctype:
                    return
                try:
                    body = await response.json()
                except Exception:
                    try:
                        text = await response.text()
                        body = json.loads(text)
                    except Exception:
                        return
                found: list[str] = []
                _walk_fife_urls(body, found)
                async with self._lock:
                    for item in found:
                        if item not in self.seen:
                            self.seen.add(item)
                            self.api_urls.append(item)
                            self.events += 1
                            print(f"[flow] network: API image URL ({len(found)} found)", flush=True)
                return

            # Only keep large final image/* payloads. Blur placeholders are square
            # ~100KB PNGs and must not be treated as the generation result.
            if ctype.startswith("image/") and any(h in url for h in IMAGE_HOST_HINTS):
                try:
                    body = await response.body()
                except Exception:
                    return
                ok, reason = image_quality_ok(body)
                if not ok:
                    return
                async with self._lock:
                    if url not in self.seen:
                        self.seen.add(url)
                        self.image_urls.append(url)
                        self.events += 1
                        print(f"[flow] network: large image response ({reason})", flush=True)
        except Exception:
            return


def extension_from_bytes(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    if data[:3] == b"\xff\xd8\xff":
        index = 2
        length = len(data)
        while index < length - 8:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0xD9):
                index += 2
                continue
            if marker == 0x00:
                index += 2
                continue
            seglen = int.from_bytes(data[index + 2 : index + 4], "big")
            if seglen < 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return width, height
            index += 2 + seglen
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        # VP8X / VP8L / VP8 — best-effort
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
    return None


def image_quality_ok(data: bytes, *, aspect: str | None = None) -> tuple[bool, str]:
    """Reject Flow blur placeholders / tiny UI assets.

    Gospel: long-form accepts landscape 16:9; shorts accept portrait 9:16.
    ``aspect=None`` uses the run-global ``_ACTIVE_ASPECT`` (set at start).
    """
    data = unwrap_image_bytes(data) if data.startswith(b"PK") else data
    extension = extension_from_bytes(data)
    if extension is None:
        return False, "not an image"
    if len(data) < MIN_IMAGE_BYTES:
        return False, f"too small ({len(data)} bytes)"
    dims = image_dimensions(data)
    if dims is None:
        # Unknown dims but large enough payload — allow cautiously.
        if len(data) >= 400_000:
            return True, f"large payload ({len(data)} bytes)"
        return False, "unknown dimensions"
    width, height = dims
    if min(width, height) < MIN_IMAGE_SIDE or max(width, height) < MIN_IMAGE_LONG_SIDE:
        return False, f"dims too small ({width}x{height})"
    # Classic Flow blur placeholder: square ~1000x1000 soft gray PNG.
    if 0.9 <= (width / max(height, 1)) <= 1.1 and len(data) < 300_000:
        return False, f"square placeholder ({width}x{height}, {len(data)} bytes)"
    ratio_hw = height / max(width, 1)
    ratio_wh = width / max(height, 1)
    # IMPORTANT: default must be None so callers inherit _ACTIVE_ASPECT.
    # A default of "portrait" was rejecting valid long-form 16:9 stills.
    want = (aspect if aspect is not None else _ACTIVE_ASPECT) or "portrait"
    want = want.strip().lower()
    if want == "landscape":
        # Reject tall portrait frames for long-form.
        if ratio_hw >= 1.15:
            return False, f"not landscape ({width}x{height})"
    else:
        # Portrait / shorts: reject wide landscape frames.
        if ratio_wh >= 1.15:
            return False, f"not portrait ({width}x{height})"
    return True, f"{width}x{height}, {len(data)} bytes"

def unwrap_image_bytes(data: bytes) -> bytes:
    """Flow often ships downloads as a ZIP containing one JPEG/PNG."""
    if extension_from_bytes(data):
        return data
    if not data.startswith(b"PK"):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir() and not info.filename.endswith("/")
            ]
            if not members:
                return data
            # Prefer real image members; otherwise take the largest file.
            image_members = []
            for info in members:
                payload = archive.read(info.filename)
                if extension_from_bytes(payload):
                    image_members.append((len(payload), payload))
            if image_members:
                image_members.sort(key=lambda item: item[0], reverse=True)
                return image_members[0][1]
            members.sort(key=lambda info: info.file_size, reverse=True)
            return archive.read(members[0].filename)
    except zipfile.BadZipFile:
        return data


def write_image_bytes(target_base: Path, data: bytes) -> Path:
    data = unwrap_image_bytes(data)
    ok, reason = image_quality_ok(data)
    if not ok:
        raise RuntimeError(f"rejected image: {reason}")
    extension = extension_from_bytes(data)
    assert extension is not None
    target = target_base.with_suffix(extension)
    if target.exists():
        target = target_base.with_name(target_base.name + "-retry").with_suffix(extension)
    target.write_bytes(data)
    print(f"[flow] accepted image ({reason})", flush=True)
    return target


async def download_url_to_path(page, url: str, target_base: Path) -> Path:
    """Fetch an image without making Playwright stream a signed CDN redirect.

    Flow's media endpoint responds quickly with a 307, but Playwright applies
    its 30-second request timeout to the entire redirected image body. On some
    connections that produces a timeout even after the CDN has returned 200.
    Resolve the authenticated redirect first, then download its signed URL with
    the standard HTTP stack and a larger timeout.
    """
    parsed = urlparse(url)
    signed_url = url
    if parsed.hostname == "labs.google":
        response = await page.context.request.get(url, timeout=15_000, max_redirects=0)
        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise RuntimeError("Flow media redirect omitted its location")
            signed_url = location
        elif not response.ok:
            raise RuntimeError(f"image redirect failed HTTP {response.status}")
        else:
            data = await response.body()
            return write_image_bytes(target_base, data)

    host = (urlparse(signed_url).hostname or "").lower()
    trusted_hosts = ("googleusercontent.com", "googleapis.com", "ggpht.com")
    trusted = host == "flow-content.google" or any(
        host == suffix or host.endswith(f".{suffix}") for suffix in trusted_hosts
    )
    if not trusted:
        raise RuntimeError(f"refusing unexpected image download host: {host or 'missing'}")
    # Use the authenticated Playwright request context for the signed CDN
    # fetch. The old urllib/thread fallback could hang indefinitely even when
    # the browser could download the same URL immediately.
    response = await page.context.request.get(
        signed_url,
        timeout=IMAGE_DOWNLOAD_TIMEOUT_S * 1000,
        max_redirects=0,
    )
    if response.status != 200:
        raise RuntimeError(f"signed image fetch failed HTTP {response.status}")
    declared = response.headers.get("content-length")
    if declared and int(declared) > MAX_IMAGE_DOWNLOAD_BYTES:
        raise RuntimeError(f"image exceeds {MAX_IMAGE_DOWNLOAD_BYTES} byte safety limit")
    data = await response.body()
    if len(data) > MAX_IMAGE_DOWNLOAD_BYTES:
        raise RuntimeError(f"image exceeds {MAX_IMAGE_DOWNLOAD_BYTES} byte safety limit")
    return write_image_bytes(target_base, data)


def _download_signed_image(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.8"},
    )
    with urllib.request.urlopen(request, timeout=IMAGE_DOWNLOAD_TIMEOUT_S) as response:
        if response.status != 200:
            raise RuntimeError(f"signed image fetch failed HTTP {response.status}")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_IMAGE_DOWNLOAD_BYTES:
            raise RuntimeError(f"image exceeds {MAX_IMAGE_DOWNLOAD_BYTES} byte safety limit")
        data = response.read(MAX_IMAGE_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_IMAGE_DOWNLOAD_BYTES:
        raise RuntimeError(f"image exceeds {MAX_IMAGE_DOWNLOAD_BYTES} byte safety limit")
    return data


def concise_error(exc: Exception) -> str:
    """Avoid logging Playwright call details, which can contain session cookies."""
    first_line = str(exc).splitlines()[0].strip()
    return first_line or type(exc).__name__


async def save_playwright_download(download, target_base: Path) -> Path:
    # Read via a temp path so we can inspect magic bytes (Flow uses .zip).
    tmp = target_base.with_suffix(".download.tmp")
    try:
        await download.save_as(str(tmp))
        data = tmp.read_bytes()
        return write_image_bytes(target_base, data)
    finally:
        if tmp.exists():
            tmp.unlink()


async def find_download_control(page):
    selectors = (
        'button[aria-label*="download" i]',
        '[role="button"][aria-label*="download" i]',
        'a[download]',
        'button:has-text("Download")',
        '[role="button"]:has-text("Download")',
        'menuitem:has-text("Download")',
        '[role="menuitem"]:has-text("Download")',
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            if await candidate.is_visible() and await candidate.is_enabled():
                return candidate
    return None


async def try_ui_download(
    page, target_base: Path, baseline_tile_ids: set[str] | None = None
) -> Path | None:
    """Flow hides Download under hover -> More. Download is often a ZIP."""
    # Hover only a newly generated tile, never an older gallery item.
    if baseline_tile_ids is not None:
        sources = await generated_tile_sources(page)
        new_ids = [tile_id for tile_id in sources if tile_id not in baseline_tile_ids]
        if not new_ids:
            return None
        images = page.locator(
            f'[data-tile-id="{new_ids[-1]}"] img[alt*="generated image" i]'
        )
    else:
        images = page.locator("img")
    for index in range(await images.count() - 1, -1, -1):
        img = images.nth(index)
        try:
            box = await img.bounding_box()
            if not box or box["width"] < 120 or box["height"] < 120:
                continue
            await img.hover(timeout=2000)
            break
        except Exception:
            continue

    more = page.get_by_role("button", name=re.compile(r"more|more options|open menu", re.I))
    for index in range(await more.count() - 1, -1, -1):
        button = more.nth(index)
        try:
            if await button.is_visible():
                await button.click(timeout=2000)
                break
        except Exception:
            continue

    control = await find_download_control(page)
    if control is None:
        return None
    try:
        async with page.expect_download(timeout=20_000) as download_info:
            await control.click()
        download = await download_info.value
        path = await save_playwright_download(download, target_base)
        print(f"[flow] UI download unpacked -> {path.name} ({path.stat().st_size} bytes)", flush=True)
        return path
    except Exception as exc:
        print(f"[flow] UI download failed: {exc}", flush=True)
        return None


async def generated_tile_sources(page) -> dict[str, str]:
    """Map rendered Flow media IDs to their image URLs.

    The gallery is virtualized, so IDs are more reliable than DOM order. The
    redirect URL is authenticated by the persistent browser context and yields
    the original image without depending on the hover menu.
    """
    result: dict[str, str] = {}
    tiles = page.locator('[data-tile-id]:has(img[alt*="generated image" i])')
    for index in range(await tiles.count()):
        tile = tiles.nth(index)
        try:
            if not await tile.is_visible():
                continue
            # Flow renders uploaded ingredients with the same data-tile-id and
            # alt text as generated media. WhatsApp references are saved with
            # this filename pattern; never treat that ingredient tile as an
            # output candidate.
            tile_text = await tile.inner_text()
            if re.search(r"\bimg-\d+\.(?:jpe?g|png|webp|bmp)\b", tile_text, re.I):
                continue
            tile_id = await tile.get_attribute("data-tile-id")
            src = await tile.locator('img[alt*="generated image" i]').first.get_attribute("src")
            if tile_id and src:
                result[tile_id] = src
        except Exception:
            continue
    return result


async def try_new_tile_download(
    page,
    baseline_tile_ids: set[str],
    target_base: Path,
    tile_attempts: dict[str, float] | None = None,
    retry_after_s: float = 60.0,
) -> Path | None:
    sources = await generated_tile_sources(page)
    now = asyncio.get_running_loop().time()
    for tile_id, src in reversed(list(sources.items())):
        if tile_id in baseline_tile_ids:
            continue
        if tile_attempts is not None and now - tile_attempts.get(tile_id, 0.0) < retry_after_s:
            continue
        url = src if src.startswith("http") else page.url.split("/fx/", 1)[0] + src
        try:
            if tile_attempts is not None:
                tile_attempts[tile_id] = now
            print(f"[flow] downloading new gallery tile {tile_id}...", flush=True)
            return await download_url_to_path(page, url, target_base)
        except Exception as exc:
            print(f"[flow] gallery tile not ready: {concise_error(exc)}", flush=True)
    return None


async def wait_and_save_image(
    page,
    capture: ImageCapture,
    target_base: Path,
    timeout_s: int,
    baseline_tile_ids: set[str],
) -> Path:
    """Wait for generation, then save via network URL or UI download."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_log = 0.0
    last_ui_try = 0.0
    ui_interval_s = 35.0
    tile_attempts: dict[str, float] = {}
    failed_since: dict[str, float] = {}
    # Let generation finish before first UI click; blur placeholders show early.
    first_ui_after_s = 45.0

    while asyncio.get_running_loop().time() < deadline:
        await ensure_generation_healthy(page)

        # Flow's authenticated media redirect is a second, stable download
        # path and survives changes to its More/Download menu.
        saved = await try_new_tile_download(
            page, baseline_tile_ids, target_base, tile_attempts
        )
        if saved is not None:
            return saved

        # Only this run's tiles count. Checked after the download attempt so a
        # generation that produced both a failure and an image still wins.
        # Flow routinely flashes "Failed" on a tile and then retries by itself,
        # reusing the same tile id, so only a failure that sticks is terminal.
        now_t = asyncio.get_running_loop().time()
        still_failed = (await failed_tile_ids(page)) - baseline_tile_ids
        failed_since = {
            tile_id: first for tile_id, first in failed_since.items() if tile_id in still_failed
        }
        for tile_id in still_failed:
            failed_since.setdefault(tile_id, now_t)
        if any(now_t - first >= FAILED_TILE_GRACE_S for first in failed_since.values()):
            raise RuntimeError(
                f"Flow marked this generation as failed for {FAILED_TILE_GRACE_S:.0f}s"
            )

        now = asyncio.get_running_loop().time()
        elapsed = timeout_s - (deadline - now)
        if now - last_log >= 10:
            remaining = int(deadline - now)
            print(
                f"[flow] waiting for image ({remaining}s left, "
                f"api={len(capture.api_urls)} large={len(capture.image_urls)} "
                f"events={capture.events})...",
                flush=True,
            )
            last_log = now

        # UI download is the reliable full-quality path (ZIP with JPEG).
        if elapsed >= first_ui_after_s and (now - last_ui_try) >= ui_interval_s:
            last_ui_try = now
            print("[flow] trying UI download (More -> Download)...", flush=True)
            saved = await try_ui_download(page, target_base, baseline_tile_ids)
            if saved is not None:
                return saved

        await page.wait_for_timeout(2000)

    print("[flow] timeout near; last UI download attempt...", flush=True)
    saved = await try_ui_download(page, target_base, baseline_tile_ids)
    if saved is not None:
        return saved
    raise TimeoutError(
        f"No usable image captured within {timeout_s}s. "
        "Generation may still be pending, or only blur placeholders appeared."
    )


async def save_debug(page, debug_dir: Path, index: int, attempt: int | None = None) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-attempt-{attempt}" if attempt is not None else ""
    await page.screenshot(
        path=str(debug_dir / f"prompt-{index:03d}{suffix}.png"), full_page=True
    )
    (debug_dir / f"prompt-{index:03d}{suffix}.html").write_text(
        await page.content(), encoding="utf-8"
    )


async def recover_flow_page(page) -> None:
    """Dismiss stale UI state and get back to an editor with a prompt box.

    A plain reload often drops onto the Flow project list (or a leftover
    delete-confirm dialog), so we re-run the same New-project bootstrap used
    at startup instead of only checking for a textarea.
    """
    try:
        await dismiss_flow_dialogs(page)
        await page.keyboard.press("Escape")
        await page.reload(wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2500)
        await ensure_not_blocked(page)
        await ensure_flow_editor_ready(page, timeout_s=60.0)
    except Exception as exc:
        raise RuntimeError(f"Flow recovery failed: {exc}") from exc


def load_profile_accounts() -> list[dict[str, Any]]:
    """Ordered Flow Gmail profiles from ``.flow/profiles.json`` (if present)."""
    if not PROFILES_MANIFEST.is_file():
        return []
    try:
        payload = json.loads(PROFILES_MANIFEST.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[flow] warning: could not read {PROFILES_MANIFEST}: {exc}", flush=True)
        return []
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in accounts:
        if not isinstance(raw, dict):
            continue
        rel = str(raw.get("dir") or "").strip()
        if not rel:
            continue
        try:
            replicas = max(1, int(raw.get("replicas") or 1))
        except (TypeError, ValueError):
            replicas = 1
        out.append(
            {
                "email": str(raw.get("email") or "").strip(),
                "dir": rel,
                "enabled": bool(raw.get("enabled", True)),
                "replicas": replicas,
                "note": str(raw.get("note") or "").strip(),
                "path": (FLOW_DIR / rel).resolve(),
            }
        )
    return out


_PROFILE_CLONE_EXCLUDES = {
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
    "DawnCache",
    "BrowserMetrics",
    "Crashpad",
    "component_crx_cache",
    "extensions_crx_cache",
}


def _clear_chromium_singletons(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = profile / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def clone_chromium_profile(source: Path, dest: Path, *, force: bool = False) -> Path:
    """Copy a logged-in profile into an isolated dir so Chrome can run in parallel.

    Chromium only allows one process per user-data-dir. Same Gmail, N workers ⇒
    N profile dirs. Replica 2+ live under ``.flow/replicas/<name>-rN``.
    """
    source = source.resolve()
    dest = dest.resolve()
    if dest == source:
        _clear_chromium_singletons(dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    cookies = dest / "Default" / "Cookies"
    if dest.is_dir() and cookies.is_file() and not force:
        _clear_chromium_singletons(dest)
        return dest
    if not source.is_dir():
        raise RuntimeError(f"source Flow profile missing: {source}")

    print(f"[flow] cloning profile {source.name} → {dest.name}", flush=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        base = Path(directory).name
        for name in names:
            if name in _PROFILE_CLONE_EXCLUDES:
                ignored.add(name)
                continue
            if name.startswith("BrowserMetrics"):
                ignored.add(name)
                continue
            # Nested cache dirs under Default/
            if base == "Default" and name in {
                "Cache",
                "Code Cache",
                "GPUCache",
                "Service Worker",
                "blob_storage",
            }:
                ignored.add(name)
        return ignored

    shutil.copytree(source, dest, ignore=_ignore, dirs_exist_ok=False)
    _clear_chromium_singletons(dest)
    return dest


def expand_worker_slots(
    *,
    replicas_override: int | None = None,
    account_selector: str | None = None,
) -> list[dict[str, Any]]:
    """Build ordered worker slots from enabled accounts × replicas.

    ``replicas_override`` (CLI) forces that many clones of the *first* enabled
    account only — useful for ``--workers 5`` all on costred without editing JSON.
    """
    accounts = [a for a in load_profile_accounts() if a.get("enabled")]
    if account_selector:
        wanted = account_selector.strip().lower()
        accounts = [
            a
            for a in accounts
            if wanted in {
                str(a.get("email") or "").strip().lower(),
                str(a.get("dir") or "").strip().lower(),
                Path(str(a.get("dir") or "")).name.strip().lower(),
            }
        ]
        if not accounts:
            raise RuntimeError(
                f"no enabled Flow account matches --profile-account {account_selector!r}"
            )
    if not accounts:
        return []

    slots: list[dict[str, Any]] = []
    if replicas_override is not None and replicas_override > 0:
        primary = accounts[0]
        for r in range(1, replicas_override + 1):
            slots.append({**primary, "replica": r, "source_path": primary["path"]})
        return slots

    for acc in accounts:
        n = max(1, int(acc.get("replicas") or 1))
        for r in range(1, n + 1):
            slots.append({**acc, "replica": r, "source_path": acc["path"]})
    return slots


def materialize_worker_profile(slot: dict[str, Any]) -> Path:
    """Return a dedicated user-data-dir for this worker slot (clone if needed)."""
    source = Path(slot["source_path"]).resolve()
    replica = int(slot.get("replica") or 1)
    if replica <= 1:
        _clear_chromium_singletons(source)
        return source
    dest = FLOW_DIR / "replicas" / f"{source.name}-r{replica}"
    return clone_chromium_profile(source, dest)


def enabled_profile_dirs() -> list[Path]:
    """Profile dirs for workers 1..N (materialized clones)."""
    return [materialize_worker_profile(slot) for slot in expand_worker_slots()]


def worker_profile_dir(
    base: Path,
    worker_number: int,
    *,
    replicas_override: int | None = None,
    account_selector: str | None = None,
) -> Path:
    """Map worker index → Chromium user-data dir.

    Prefer ``.flow/profiles.json`` allowlist (+ optional replicas) so a flagged
    Gmail never becomes worker 1 by accident. Same account can fan out to N
    clones (``replicas`` or CLI ``--replicas``). Legacy fallback: ``base``,
    ``base-2``, ``base-3``.
    """
    if worker_number < 1:
        raise ValueError(f"worker_number must be >= 1, got {worker_number}")

    slots = expand_worker_slots(
        replicas_override=replicas_override,
        account_selector=account_selector,
    )
    if slots:
        if worker_number > len(slots):
            accounts = load_profile_accounts()
            disabled = [
                a.get("email") or a.get("dir")
                for a in accounts
                if not a.get("enabled")
            ]
            raise RuntimeError(
                f"only {len(slots)} worker slot(s) from {PROFILES_MANIFEST} "
                f"(enabled accounts × replicas), but --workers={worker_number}. "
                f"Raise account ``replicas``, pass --replicas {worker_number}, "
                f"or reduce --workers. Disabled: {disabled or 'none'}"
            )
        return materialize_worker_profile(slots[worker_number - 1])

    # Legacy layout: worker 1 = base, worker 2 = base-2, …
    if worker_number == 1:
        return base
    return base.with_name(f"{base.name}-{worker_number}")


async def process_prompt(
    *,
    worker_number: int,
    page,
    capture: ImageCapture,
    index: int,
    prompt: str,
    prompt_count: int,
    args: argparse.Namespace,
    aspect: str,
    images_dir: Path,
    debug_dir: Path,
    project_dir: Path,
    state_path: Path,
    state: dict[str, Any],
    state_lock: asyncio.Lock,
) -> None:
    prefix = f"[flow:w{worker_number}]"
    print(f"{prefix} submitting {index}/{prompt_count}", flush=True)
    target = None
    last_error: Exception | None = None
    for attempt in range(1, args.retries + 2):
        try:
            capture.reset()
            print(
                f"[flow] opening a fresh Flow project for attempt {attempt}",
                flush=True,
            )
            await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            await ensure_flow_editor_ready(page, timeout_s=45.0)
            await ensure_single_image_output(page, aspect=aspect)
            await attach_reference_images(page, getattr(args, "references", []))
            await fill_prompt(page, prompt)
            # Everything already on screen — earlier media, failures, and the
            # ingredient we just uploaded — is excluded by identity from here on.
            baseline_tile_ids = await all_tile_ids(page)
            print(f"[flow] baseline gallery tiles: {len(baseline_tile_ids)}", flush=True)
            # Navigation/editor setup can emit responses for previously visible
            # gallery tiles. Do not let one of those stale URLs win before the
            # response from this Generate click arrives.
            capture.reset()
            await click_generate(page, baseline_tile_ids)
            print(
                f"{prefix} generate clicked; waiting for result "
                f"(attempt {attempt}/{args.retries + 1})...",
                flush=True,
            )
            # Prefer labeled stem from manifest (beat-041-strawberry); fallback 01.
            stem = f"{index:02d}"
            stems = getattr(args, "_prompt_stems", None) or []
            if 1 <= index <= len(stems):
                stem = stems[index - 1] or stem
            target_base = images_dir / stem
            target = await wait_and_save_image(
                page, capture, target_base, args.timeout, baseline_tile_ids
            )
            break
        except Exception as exc:
            last_error = exc
            try:
                await save_debug(page, debug_dir, index, attempt)
            except Exception as debug_exc:
                print(f"{prefix} could not save diagnostics: {debug_exc}", flush=True)
            if attempt > args.retries:
                break
            print(
                f"{prefix} attempt {attempt} failed: {exc}; "
                "reloading Flow and retrying...",
                flush=True,
            )
            try:
                await recover_flow_page(page)
            except Exception as recovery_exc:
                last_error = recovery_exc
                break

    state_keys = getattr(args, "_prompt_keys", None) or []
    state_key = state_keys[index - 1] if 1 <= index <= len(state_keys) else f"idx:{index}"
    async with state_lock:
        if target is None:
            assert last_error is not None
            state["failed"][state_key] = str(last_error)
            save_state(state_path, state)
            production.mark_asset_failed(
                project_dir, state_key, f"AI generation failed for {state_key}"
            )
        else:
            state["completed"][state_key] = str(target.relative_to(project_dir))
            state["failed"].pop(state_key, None)
            save_state(state_path, state)
            production.mark_asset_completed(project_dir, state_key, target)

    if target is None:
        raise RuntimeError(
            f"prompt {index} failed after {args.retries + 1} attempts: "
            f"{last_error}. Debug files: {debug_dir}"
        ) from last_error

    print(f"{prefix} saved {target}", flush=True)
    await page.wait_for_timeout(args.delay * 1000)


async def run(args: argparse.Namespace) -> int:
    project_dir = resolve_project_dir(args.slug, args.project_dir)
    images_dir = project_dir / "media" / "images"
    prompt_file = Path(args.prompt_file).expanduser() if args.prompt_file else images_dir / "prompts.plain.txt"
    state_path = project_dir / "media" / "flow-run.json"
    debug_dir = project_dir / "media" / "flow-debug"
    images_dir.mkdir(parents=True, exist_ok=True)
    prompts = read_prompts(prompt_file)
    args._prompt_stems = read_prompt_stems(
        images_dir / "prompts.manifest.json", len(prompts)
    )
    args._prompt_keys = read_prompt_keys(
        images_dir / "prompts.manifest.json", len(prompts)
    )
    state = load_state(state_path)
    stale = migrate_state_keys(state, args._prompt_keys, project_dir)
    if stale:
        save_state(state_path, state)
        print(
            f"[flow] dropped {stale} resume entries from an older prompt pack "
            "(their images no longer match this pack); those prompts will re-run",
            flush=True,
        )
    aspect = set_active_aspect(resolve_aspect(project_dir, getattr(args, "aspect", None)))

    pending = [
        (index, prompt)
        for index, prompt in enumerate(prompts, start=1)
        if args._prompt_keys[index - 1] not in state["completed"]
    ]
    if args.start > 1:
        pending = [(index, prompt) for index, prompt in pending if index >= args.start]
    if args.limit:
        pending = pending[: args.limit]

    # The driver creates this state before Flow starts.  Creating it here too
    # keeps the standalone Flow command visible on the same live dashboard.
    try:
        production.refresh_from_media_plan(
            project_dir,
            phase="gathering",
            message=f"Generating AI stills ({len(prompts) - len(pending)}/{len(prompts)})",
        )
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    print(f"[flow] project: {project_dir}")
    print(f"[flow] aspect:  {aspect} ({'16:9 landscape' if aspect == 'landscape' else '9:16 portrait'})")
    print(f"[flow] prompts: {prompt_file} ({len(prompts)} total, {len(pending)} pending)")
    if args._prompt_stems[:3]:
        print(f"[flow] names:   {', '.join(args._prompt_stems[:3])}…", flush=True)
    if args.dry_run:
        for index, prompt in pending:
            print(f"{index:03d}: {prompt}")
        return 0
    if not pending:
        print("[flow] nothing to do")
        return 0

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is missing. Run: uv sync --extra flow", file=sys.stderr)
        return 1

    profile_dir = Path(args.profile_dir).expanduser()
    browser = resolve_browser()
    print(f"[flow] browser: {browser['label']}")
    print(f"[flow] workers: {args.workers}")
    cdp_url = getattr(args, "cdp_url", None)
    if cdp_url and args.workers != 1:
        raise RuntimeError("--cdp-url supports exactly one worker")

    async with async_playwright() as playwright:
        # Google shows "This browser or app may not be secure" when Playwright
        # launches with --enable-automation / navigator.webdriver. Drop those
        # markers so the user can finish their own interactive login.
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        launch_kwargs = {
            "headless": args.headless,
            "accept_downloads": True,
            "locale": "en-US",
            "ignore_default_args": ["--enable-automation"],
            "args": browser_args,
        }
        if args.headless:
            launch_kwargs["viewport"] = {"width": 1440, "height": 1000}
        else:
            # Fixed viewport + browser chrome overflows 1080p and drifts off-screen.
            # Maximize and let the page fill the real window.
            launch_kwargs["no_viewport"] = True
            browser_args.extend(
                [
                    "--start-maximized",
                    "--window-position=0,0",
                ]
            )
        if "channel" in browser:
            launch_kwargs["channel"] = browser["channel"]
        if "executable_path" in browser:
            launch_kwargs["executable_path"] = browser["executable_path"]
        contexts = []
        pages = []
        captures = []
        try:
            if cdp_url:
                print(f"[flow] attaching to persistent browser: {cdp_url}", flush=True)
                attached_browser = await playwright.chromium.connect_over_cdp(
                    cdp_url, timeout=15_000
                )
                attached_contexts = attached_browser.contexts
                if not attached_contexts:
                    raise RuntimeError("persistent Flow browser has no browser context")
                context = attached_contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(90_000)
                page.set_default_navigation_timeout(90_000)
                pages.append(page)
                capture = ImageCapture()
                capture.attach(page)
                captures.append(capture)

            # Fail fast if the allowlist/replicas cannot satisfy --workers.
            replicas_override = getattr(args, "replicas", None)
            account_selector = getattr(args, "profile_account", None)
            slots = expand_worker_slots(
                replicas_override=replicas_override,
                account_selector=account_selector,
            )
            if PROFILES_MANIFEST.is_file():
                print(f"[flow] profiles: {PROFILES_MANIFEST}", flush=True)
            if slots:
                print(
                    f"[flow] worker slots: {len(slots)} "
                    f"(accounts × replicas; override={replicas_override!r})",
                    flush=True,
                )
            for worker_number in range(1, args.workers + 1):
                if cdp_url:
                    break
                this_profile = worker_profile_dir(
                    profile_dir,
                    worker_number,
                    replicas_override=replicas_override,
                    account_selector=account_selector,
                )
                this_profile.mkdir(parents=True, exist_ok=True)
                slot = slots[worker_number - 1] if worker_number <= len(slots) else {}
                email = (slot or {}).get("email") or ""
                replica = (slot or {}).get("replica") or 1
                label = f" ({email} r{replica})" if email else ""
                print(
                    f"[flow] worker {worker_number} profile: {this_profile}{label}",
                    flush=True,
                )
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(this_profile), **launch_kwargs
                )
                contexts.append(context)
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = context.pages[0] if context.pages else await context.new_page()
                pages.append(page)
                page.set_default_timeout(90_000)
                page.set_default_navigation_timeout(90_000)
                if not args.headless:
                    await ensure_window_onscreen(page)
                # Headless + multi-worker cold starts can exceed Playwright's 30s default.
                last_nav_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        await page.goto(
                            FLOW_URL,
                            wait_until="domcontentloaded",
                            timeout=90_000,
                        )
                        last_nav_error = None
                        break
                    except Exception as exc:
                        last_nav_error = exc
                        print(
                            f"[flow] worker {worker_number}: goto attempt "
                            f"{attempt}/3 failed: {exc}",
                            flush=True,
                        )
                        await page.wait_for_timeout(2_000 * attempt)
                if last_nav_error is not None:
                    raise last_nav_error
                await page.wait_for_timeout(3000)
                capture = ImageCapture()
                capture.attach(page)
                captures.append(capture)

            if not args.headless:
                print("[flow] Complete Google login in EACH browser window if needed.")
                print(
                    "[flow] After login, press Enter here — the runner will "
                    "auto-dismiss dialogs and click New project / open the editor."
                )
                print("[flow] Each worker profile persists its own Gmail session.")
                print("[flow] Do not give this tool your password.")
                input()
            # Headless and headed both need this: landing page is the project list,
            # often behind a delete-confirm modal; prompt box only exists in-editor.
            for worker_number, page in enumerate(pages, start=1):
                print(
                    f"[flow] worker {worker_number}: ensuring Flow editor is ready…",
                    flush=True,
                )
                await ensure_not_blocked(page)
                await ensure_flow_editor_ready(page, timeout_s=90.0)

            queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
            for item in pending:
                queue.put_nowait(item)
            state_lock = asyncio.Lock()
            failures: list[Exception] = []

            async def worker(worker_index: int) -> None:
                page = pages[worker_index]
                capture = captures[worker_index]
                while not queue.empty():
                    try:
                        index, prompt = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await process_prompt(
                            worker_number=worker_index + 1,
                            page=page,
                            capture=capture,
                            index=index,
                            prompt=prompt,
                            prompt_count=len(prompts),
                            args=args,
                            aspect=aspect,
                            images_dir=images_dir,
                            debug_dir=debug_dir,
                            project_dir=project_dir,
                            state_path=state_path,
                            state=state,
                            state_lock=state_lock,
                        )
                    except Exception as exc:
                        failures.append(exc)
                        return
                    finally:
                        queue.task_done()

            await asyncio.gather(*(worker(i) for i in range(args.workers)))
            if failures:
                raise RuntimeError(
                    f"{len(failures)} worker(s) stopped; first error: {failures[0]}"
                ) from failures[0]
        finally:
            for capture, page in zip(captures, pages):
                capture.detach(page)
            await asyncio.gather(*(context.close() for context in contexts), return_exceptions=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--project-dir")
    parser.add_argument("--prompt-file")
    parser.add_argument(
        "--reference",
        dest="references",
        action="append",
        default=[],
        help="local reference image to attach as a Flow ingredient; repeat for multiple images",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(ROOT / ".flow" / "chromium-profile"),
        help="persistent browser profile; contains cookies, keep it private",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="parallel Chrome windows (each needs its own profile dir / replica)",
    )
    parser.add_argument(
        "--replicas",
        type=int,
        default=None,
        help=(
            "clone the first enabled .flow/profiles.json account this many times "
            "(e.g. --workers 5 --replicas 5 all on costred366). Overrides per-account "
            "replicas in the manifest."
        ),
    )
    parser.add_argument(
        "--profile-account",
        help="enabled Flow account directory or email from .flow/profiles.json",
    )
    parser.add_argument(
        "--cdp-url",
        help="attach to an already-running persistent Chromium (for server-side Flow sessions)",
    )
    parser.add_argument("--headless", action="store_true", help="use only after headed login works")
    parser.add_argument(
        "--aspect",
        choices=("landscape", "portrait", "auto"),
        default="auto",
        help="gospel: long-form=landscape 16:9, shorts=portrait 9:16 (auto from project.json / path)",
    )
    parser.add_argument("--start", type=int, default=1, help="first prompt number to process")
    parser.add_argument("--limit", type=int, default=0, help="maximum prompts for this run")
    parser.add_argument("--timeout", type=int, default=900, help="generation timeout per prompt in seconds")
    parser.add_argument("--delay", type=int, default=5, help="delay between prompts in seconds")
    parser.add_argument(
        "--retries", type=int, default=2, help="retries per prompt after transient Flow errors"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.replicas is not None and args.replicas < 1:
        parser.error("--replicas must be at least 1 when set")
    if args.replicas is not None and args.replicas < args.workers:
        parser.error(
            f"--replicas ({args.replicas}) must be >= --workers ({args.workers}) "
            "when forcing one-account fan-out"
        )
    if getattr(args, "aspect", None) == "auto":
        args.aspect = None
    try:
        if args.dry_run:
            return asyncio.run(run(args))
        project_dir = resolve_project_dir(args.slug, args.project_dir)
        with exclusive_flow_browser():
            with exclusive_project_run(project_dir):
                return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[flow] stopped", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[flow] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
