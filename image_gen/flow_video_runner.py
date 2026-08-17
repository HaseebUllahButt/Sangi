#!/usr/bin/env python3
"""Submit a single video prompt to Google Flow through Chromium.

Mirror of ``flow_runner.py`` but for AI video generation. It reuses the image
runner's browser bootstrap, dialog handling, reference upload and generate
click helpers (imported from the picture script, which stays untouched), while
switching the composer to Video mode and capturing/downloading mp4/webm output.

Usage from generate_video.py (the normal entry point):
    uv run python scripts/flow_video_runner.py --slug job --project-dir JOB \
        --prompt-file prompts.plain.txt --profile-dir DIR --headless --cdp-url URL
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import flow_runner as fr  # noqa: E402

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
MIN_VIDEO_BYTES = 100_000
MAX_VIDEO_DOWNLOAD_BYTES = 500_000_000
VIDEO_DOWNLOAD_TIMEOUT_S = 300
VIDEO_MODE_PATTERNS = (
    re.compile(r"^\s*Video\s*$", re.I),
    re.compile(r"Text to Video", re.I),
    re.compile(r"Frames to Video", re.I),
    re.compile(r"Video to Video", re.I),
    re.compile(r"^\s*Videos\s*$", re.I),
)
VIDEO_MODEL_MARKERS = ("veo", "video", "nano banana 3")
# Flow keeps the composer on the image model (Nano Banana 2 Lite) by default,
# with the video option tucked behind the Create (add_2) menu. Verified against
# the live Flow UI: the submit control is the arrow_forward button, and opening
# the model settings chip reveals the actual model name.
CREATE_BUTTON_SELECTORS = (
    'button:has(i.google-symbols:text-is("add_2"))',
    "button:has-text('Create')",
    'button[aria-haspopup="dialog"]:has(i.google-symbols)',
)


def _walk_video_urls(node: Any, found: list[str]) -> None:
    """Collect candidate video URLs from Flow API JSON."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in VID_KEYS and isinstance(value, str) and value.startswith("https://"):
                if any(h in value for h in fr.IMAGE_HOST_HINTS) or value.rstrip().lower().endswith(
                    (".mp4", ".webm", ".mov")
                ):
                    found.append(value)
            else:
                _walk_video_urls(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_video_urls(item, found)


VID_KEYS = frozenset(
    ("fifeUrl", "fife_url", "url", "videoUrl", "video_url", "video", "streamUrl", "downloadUrl", "mp4")
)


def video_extension_from_bytes(data: bytes) -> str | None:
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if len(data) >= 12 and data[4:8] == b"moov":
        return ".moov"
    return None


def video_quality_ok(data: bytes) -> tuple[bool, str]:
    if len(data) < MIN_VIDEO_BYTES:
        return False, f"too small ({len(data)} bytes)"
    ext = video_extension_from_bytes(data)
    if ext is None:
        return False, "no mp4/webm magic bytes"
    return True, f"{ext} {len(data)} bytes"


def unwrap_video_bytes(data: bytes) -> bytes:
    """Flow sometimes ships a download as a ZIP containing the media file."""
    if video_extension_from_bytes(data) is not None:
        return data
    if not data.startswith(b"PK"):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            if not members:
                return data
            candidates = []
            for info in members:
                payload = archive.read(info.filename)
                if video_extension_from_bytes(payload) is not None:
                    candidates.append((info.file_size, payload))
            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                return candidates[0][1]
            members.sort(key=lambda info: info.file_size, reverse=True)
            return archive.read(members[0].filename)
    except zipfile.BadZipFile:
        return data


def write_video_bytes(target_base: Path, data: bytes) -> Path:
    data = unwrap_video_bytes(data)
    ok, reason = video_quality_ok(data)
    if not ok:
        raise RuntimeError(f"rejected video: {reason}")
    ext = video_extension_from_bytes(data)
    assert ext is not None
    target = target_base.with_suffix(ext)
    if target.exists():
        target = target_base.with_name(target_base.name + "-retry").with_suffix(ext)
    target.write_bytes(data)
    print(f"[flow] accepted video ({reason})", flush=True)
    return target


async def open_create_menu(page) -> bool:
    """Open Flow's composer mode menu (add_2 "Create" in the prompt row)."""
    for selector in CREATE_BUTTON_SELECTORS:
        try:
            loc = page.locator(selector)
            for index in range(await loc.count() - 1, -1, -1):
                button = loc.nth(index)
                if not await button.is_visible():
                    continue
                await button.click(timeout=3000)
                await page.wait_for_timeout(500)
                print("[flow] opened composer Create menu", flush=True)
                return True
        except Exception:
            continue
    return False


async def active_model_name(page) -> str:
    """Best-effort read of the composer model chip text (lowercased)."""
    try:
        return (await page.locator("body").inner_text(timeout=500) or "").lower()
    except Exception:
        return ""


def _model_chip_selector() -> str:
    """The composer model chip opens a Radix popover with the mode tabs.

    Verified against the live UI: the chip is the ``button[aria-haspopup=menu]``
    whose text shows the active model — "🍌 Nano Banana 2 Lite crop_9_16 x1" in
    Image mode, "Video · 8s crop_9_16 x1" in Video mode.
    """
    return 'button[aria-haspopup="menu"]'


def _video_tab_selector() -> str:
    """Radix tab that switches the composer to Video mode (id ends -trigger-VIDEO)."""
    return '[role="tab"][id$="-trigger-VIDEO"]'


def _image_tab_selector() -> str:
    """Radix tab that switches the composer to Image mode (id ends -trigger-IMAGE)."""
    return '[role="tab"][id$="-trigger-IMAGE"]'


async def find_model_chip(page):
    """Return the visible composer model chip button, or None."""
    selector = _model_chip_selector()
    chips = page.locator(selector)
    for i in range(await chips.count()):
        chip = chips.nth(i)
        try:
            if not await chip.is_visible():
                continue
            text = re.sub(r"\s+", " ", (await chip.inner_text())).strip()
            if any(m in text for m in ("Nano", "Banana", "Video", "Veo", "Omni", "Gemini", "Imagen", "x1", "x2")):
                return chip
        except Exception:
            continue
    return None


async def switch_composer_tab(page, mode: str) -> bool:
    """Open the model chip popover and click the Image or Video tab.

    Returns True once the composer reflects the requested mode.
    """
    chip = await find_model_chip(page)
    if chip is None:
        return False
    try:
        await chip.click(timeout=4000)
        await page.wait_for_timeout(1200)
    except Exception:
        return False
    selector = _video_tab_selector() if mode == "video" else _image_tab_selector()
    tab = page.locator(selector)
    if not await tab.count():
        await page.keyboard.press("Escape")
        return False
    try:
        await tab.first.click(timeout=5000)
        await page.wait_for_timeout(1200)
    except Exception:
        await page.keyboard.press("Escape")
        return False
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)
    return True


async def ensure_video_generation_mode(page) -> None:
    """Switch the Flow composer from Image to Video generation.

    The current Flow UI defaults to ``Nano Banana`` (image model). Video/Veo is
    behind the composer model chip: clicking it opens a Radix popover whose
    ``role=tab`` slider holds "image Image" and "videocam Video" entries. Prior
    code only clicked visible ``role=button`` elements and the add_2 Create menu,
    so a fresh page stayed on Nano Banana and every run failed before generate.
    """
    box = await fr.find_prompt_box(page)
    if box is not None:
        try:
            placeholder = (await box.get_attribute("placeholder") or "").lower()
            aria = (await box.get_attribute("aria-label") or "").lower()
            if "video" in placeholder or "video" in aria:
                return
        except Exception:
            pass

    # Chip already advertises Video/Veo/Omni (a video-capable model).
    text = await active_model_name(page)
    compact = text.replace(" ", "")
    if not any(m in compact for m in ("nanobanana2", "nanobanana")):
        if re.search(r"video|veo|omni", text, re.I):
            return

    # Option 1: open the model chip popover and click the Video tab.
    if await switch_composer_tab(page, "video"):
        print("[flow] switched to Video mode via model chip tab", flush=True)
        await page.wait_for_timeout(800)
        return

    # Option 2: a visible Video-type button already on screen.
    for pattern in VIDEO_MODE_PATTERNS:
        if await fr._click_visible_button(page, pattern):
            print(f"[flow] switched to Video mode via {pattern.pattern}", flush=True)
            await page.wait_for_timeout(800)
            return

    # Option 3: open the composer Create menu and pick a video entry.
    if await open_create_menu(page):
        for pattern in VIDEO_MODE_PATTERNS:
            if await fr._click_visible_button(page, pattern):
                print(f"[flow] switched to Video mode via Create menu ({pattern.pattern})", flush=True)
                await page.wait_for_timeout(1200)
                return
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

    # Final check: model chip should no longer advertise the stills model.
    text = await active_model_name(page)
    if "nanobanana" in text.replace(" ", "") or "nano banana 2" in text:
        raise RuntimeError(
            "Flow composer is still on the image model (Nano Banana). "
            "Switch it to Video/Veo manually and rerun."
        )


async def ensure_video_editor_ready(page, *, timeout_s: float = 60.0) -> None:
    """Like flow_runner.ensure_flow_editor_ready but stays in Video mode."""
    await fr.dismiss_drop_overlay(page)
    await fr.ensure_not_blocked(page)
    await fr.dismiss_flow_dialogs(page)

    if "/project/" not in page.url:
        for pattern in (
            re.compile(r"^Create with Google Flow$", re.I),
            re.compile(r"^Try in Google Flow$", re.I),
        ):
            if await fr._click_visible_button(page, pattern, exact=True):
                await page.wait_for_timeout(2500)
                break
        if "accounts.google.com" in page.url:
            raise RuntimeError(
                "Google login is required for this Flow profile; complete it in a headed run first."
            )

    if await fr.find_prompt_box(page) is not None:
        await ensure_video_generation_mode(page)
        if await fr.find_prompt_box(page) is not None:
            return

    print("[flow] prompt box not ready; opening a Flow project…", flush=True)
    opened = await fr.click_new_project(page)
    if not opened:
        try:
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
        await fr.dismiss_flow_dialogs(page)
        opened = await fr.click_new_project(page)

    deadline = time.monotonic() + max(5.0, timeout_s)
    retried_new = False
    while time.monotonic() < deadline:
        await fr.ensure_not_blocked(page)
        await fr.dismiss_flow_dialogs(page)
        try:
            await ensure_video_generation_mode(page)
        except RuntimeError:
            pass
        if await fr.find_prompt_box(page) is not None:
            print("[flow] editor ready (prompt box visible)", flush=True)
            return
        if not retried_new and time.monotonic() > deadline - (timeout_s * 0.45):
            retried_new = True
            await fr.click_new_project(page)
        await page.wait_for_timeout(800)

    raise RuntimeError(
        "Could not reach a Flow editor with a prompt box in Video mode. "
        "Dismiss any dialogs, open a project, confirm Video/Veo mode, then rerun."
    )


async def fill_video_prompt(page, prompt: str) -> None:
    """fill_prompt equivalent that does not flip the composer back to Image."""
    await ensure_video_editor_ready(page, timeout_s=45.0)
    box = await fr.find_prompt_box(page)
    if box is None:
        raise RuntimeError("Could not find the Flow prompt box in Video mode")
    await box.click()
    tag = await box.evaluate("el => el.tagName.toLowerCase()")
    if tag == "textarea" or tag == "input":
        await box.fill(prompt)
    else:
        await box.press("Control+A")
        await page.keyboard.insert_text(prompt)


class VideoCapture:
    """Collect generated video URLs / bodies from Flow network traffic."""

    def __init__(self) -> None:
        self.video_urls: list[str] = []
        self.video_bodies: list[tuple[bytes, str]] = []
        self.seen: set[str] = set()
        self.events = 0
        self._lock = asyncio.Lock()

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def detach(self, page) -> None:
        try:
            page.remove_listener("response", self._on_response)
        except Exception:
            pass

    def reset(self) -> None:
        self.video_urls.clear()
        self.video_bodies.clear()
        self.seen.clear()
        self.events = 0

    async def _on_response(self, response) -> None:
        try:
            url = response.url or ""
            status = response.status
            if status != 200:
                return
            ctype = (response.headers or {}).get("content-type", "").lower()
            lowered = url.lower()

            is_api = (
                "generatevideo" in lowered
                or "flowMedia" in url
                or "aisandbox" in url
                or "veo" in lowered
                or "batchGenerate" in url
            )
            if is_api:
                if ctype and any(k in ctype for k in ("json", "text", "javascript")):
                    try:
                        body = await response.json()
                    except Exception:
                        try:
                            body = json.loads(await response.text())
                        except Exception:
                            return
                    found: list[str] = []
                    _walk_video_urls(body, found)
                    async with self._lock:
                        for item in found:
                            if item not in self.seen:
                                self.seen.add(item)
                                self.video_urls.append(item)
                                self.events += 1
                                print(f"[flow] network: potential video URL ({len(found)} found)", flush=True)
            elif ctype.startswith("video/") or lowered.endswith((".mp4", ".webm", ".mov")):
                try:
                    payload = await response.body()
                except Exception:
                    return
                ok, reason = video_quality_ok(payload)
                if not ok:
                    return
                async with self._lock:
                    if url not in self.seen:
                        self.seen.add(url)
                        self.video_bodies.append((payload, reason))
                        self.events += 1
                        print(f"[flow] network: large video response ({reason})", flush=True)
        except Exception:
            return


async def generated_video_sources(page) -> dict[str, str]:
    """Map rendered Flow video tiles to their media srcs."""
    result: dict[str, str] = {}
    tiles = page.locator("[data-tile-id]:has(video)")
    for index in range(await tiles.count()):
        tile = tiles.nth(index)
        try:
            if not await tile.is_visible():
                continue
            tile_text = await tile.inner_text()
            if re.search(r"\bimg-\d+\.(?:jpe?g|png|webp|bmp)\b", tile_text, re.I):
                continue
            tile_id = await tile.get_attribute("data-tile-id")
            video = tile.locator("video").first
            src = await video.get_attribute("src")
            if not src and (await video.locator("source").count()):
                src = await video.locator("source").first.get_attribute("src")
            if tile_id and src:
                result[tile_id] = src
        except Exception:
            continue
    return result


async def download_video_url_to_path(page, url: str, target_base: Path) -> Path:
    """Fetch a video URL, resolving Flow's authenticated media redirect first."""
    from urllib.parse import urlparse

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
            raise RuntimeError(f"video redirect failed HTTP {response.status}")
        else:
            data = await response.body()
            return write_video_bytes(target_base, data)

    host = (urlparse(signed_url).hostname or "").lower()
    trusted_hosts = ("googleusercontent.com", "googleapis.com", "ggpht.com")
    trusted = host == "flow-content.google" or any(
        host == suffix or host.endswith(f".{suffix}") for suffix in trusted_hosts
    )
    if not trusted:
        raise RuntimeError(f"refusing unexpected video download host: {host or 'missing'}")
    response = await page.context.request.get(
        signed_url,
        timeout=VIDEO_DOWNLOAD_TIMEOUT_S * 1000,
        max_redirects=0,
    )
    if response.status != 200:
        raise RuntimeError(f"signed video fetch failed HTTP {response.status}")
    declared = response.headers.get("content-length")
    if declared and int(declared) > MAX_VIDEO_DOWNLOAD_BYTES:
        raise RuntimeError(f"video exceeds {MAX_VIDEO_DOWNLOAD_BYTES} byte safety limit")
    data = await response.body()
    if len(data) > MAX_VIDEO_DOWNLOAD_BYTES:
        raise RuntimeError(f"video exceeds {MAX_VIDEO_DOWNLOAD_BYTES} byte safety limit")
    return write_video_bytes(target_base, data)


async def try_video_tile_download(
    page,
    baseline_tile_ids: set[str],
    target_base: Path,
    tile_attempts: dict[str, float] | None = None,
    retry_after_s: float = 60.0,
) -> Path | None:
    sources = await generated_video_sources(page)
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
            print(f"[flow] downloading new video tile {tile_id}...", flush=True)
            return await download_video_url_to_path(page, url, target_base)
        except Exception as exc:
            print(f"[flow] video tile not ready: {fr.concise_error(exc)}", flush=True)
    return None


async def try_video_ui_download(
    page, target_base: Path, baseline_tile_ids: set[str] | None = None
) -> Path | None:
    """Hover the generated video tile -> More -> Download."""
    if baseline_tile_ids is not None:
        sources = await generated_video_sources(page)
        new_ids = [tile_id for tile_id in sources if tile_id not in baseline_tile_ids]
        if not new_ids:
            return None
        videos = page.locator(f'[data-tile-id="{new_ids[-1]}"] video')
    else:
        videos = page.locator("video")
    for index in range(await videos.count() - 1, -1, -1):
        video = videos.nth(index)
        try:
            box = await video.bounding_box()
            if not box or box["width"] < 120 or box["height"] < 120:
                continue
            await video.hover(timeout=2000)
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

    control = await fr.find_download_control(page)
    if control is None:
        return None
    try:
        async with page.expect_download(timeout=30_000) as download_info:
            await control.click()
        download = await download_info.value
        tmp = target_base.with_suffix(".download.tmp")
        try:
            await download.save_as(str(tmp))
            data = tmp.read_bytes()
            path = write_video_bytes(target_base, data)
        finally:
            if tmp.exists():
                tmp.unlink()
        print(f"[flow] UI video download unpacked -> {path.name} ({path.stat().st_size} bytes)", flush=True)
        return path
    except Exception as exc:
        print(f"[flow] UI video download failed: {fr.concise_error(exc)}", flush=True)
        return None


async def wait_and_save_video(
    page,
    capture: VideoCapture,
    target_base: Path,
    timeout_s: int,
    baseline_tile_ids: set[str],
) -> Path:
    """Wait for generation, then save via network URL or UI download."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_log = 0.0
    last_ui_try = 0.0
    last_url_try = 0.0
    ui_interval_s = 60.0
    url_interval_s = 20.0
    tile_attempts: dict[str, float] = {}
    failed_since: dict[str, float] = {}
    attempted_urls: set[str] = set()
    first_ui_after_s = 90.0

    while asyncio.get_running_loop().time() < deadline:
        await fr.ensure_generation_healthy(page)

        saved = await try_video_tile_download(
            page, baseline_tile_ids, target_base, tile_attempts
        )
        if saved is not None:
            return saved

        now_t = asyncio.get_running_loop().time()
        still_failed = (await fr.failed_tile_ids(page)) - baseline_tile_ids
        failed_since = {
            tile_id: first for tile_id, first in failed_since.items() if tile_id in still_failed
        }
        for tile_id in still_failed:
            failed_since.setdefault(tile_id, now_t)
        if any(now_t - first >= fr.FAILED_TILE_GRACE_S for first in failed_since.values()):
            raise RuntimeError(
                f"Flow marked this video generation as failed for {fr.FAILED_TILE_GRACE_S:.0f}s"
            )

        now = asyncio.get_running_loop().time()
        elapsed = timeout_s - (deadline - now)
        if now - last_log >= 10:
            remaining = int(deadline - now)
            print(
                f"[flow] waiting for video ({remaining}s left, "
                f"urls={len(capture.video_urls)} bodies={len(capture.video_bodies)} "
                f"events={capture.events})...",
                flush=True,
            )
            last_log = now

        if capture.video_bodies and now - last_url_try >= 5:
            last_url_try = now
            body, reason = capture.video_bodies[-1]
            try:
                return write_video_bytes(target_base, body)
            except Exception as exc:
                print(f"[flow] network video body rejected: {fr.concise_error(exc)}", flush=True)
                capture.video_bodies.clear()

        if elapsed >= 30 and now - last_url_try >= url_interval_s:
            last_url_try = now
            for url in list(capture.video_urls):
                if url in attempted_urls:
                    continue
                attempted_urls.add(url)
                try:
                    return await download_video_url_to_path(page, url, target_base)
                except Exception as exc:
                    print(f"[flow] video URL download rejected: {fr.concise_error(exc)}", flush=True)

        if elapsed >= first_ui_after_s and (now - last_ui_try) >= ui_interval_s:
            last_ui_try = now
            print("[flow] trying video UI download (More -> Download)...", flush=True)
            saved = await try_video_ui_download(page, target_base, baseline_tile_ids)
            if saved is not None:
                return saved

        await page.wait_for_timeout(2000)

    print("[flow] timeout near; last UI download attempt...", flush=True)
    saved = await try_video_ui_download(page, target_base, baseline_tile_ids)
    if saved is not None:
        return saved
    raise TimeoutError(
        f"No usable video captured within {timeout_s}s. "
        "Generation may still be pending, or Veo is still rendering."
    )


async def process_video_prompt(
    worker_number: int,
    page,
    capture: VideoCapture,
    index: int,
    prompt: str,
    prompt_count: int,
    args: argparse.Namespace,
    videos_dir: Path,
    debug_dir: Path,
    project_dir: Path,
) -> Path:
    prefix = f"[flow:w{worker_number}]"
    print(f"{prefix} submitting video prompt {index}/{prompt_count}", flush=True)
    target = None
    last_error: Exception | None = None
    for attempt in range(1, args.retries + 2):
        try:
            capture.reset()
            print(f"[flow] opening a fresh Flow project for attempt {attempt}", flush=True)
            await page.goto(fr.FLOW_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            await ensure_video_editor_ready(page, timeout_s=45.0)
            await fr.attach_reference_images(page, getattr(args, "references", []))
            await fill_video_prompt(page, prompt)
            baseline_tile_ids = await fr.all_tile_ids(page)
            print(f"[flow] baseline gallery tiles: {len(baseline_tile_ids)}", flush=True)
            capture.reset()
            await fr.click_generate(page, baseline_tile_ids)
            print(
                f"{prefix} generate clicked; waiting for video result "
                f"(attempt {attempt}/{args.retries + 1})...",
                flush=True,
            )
            stem = f"{index:02d}"
            target_base = videos_dir / stem
            target = await wait_and_save_video(
                page, capture, target_base, args.timeout, baseline_tile_ids
            )
            break
        except Exception as exc:
            last_error = exc
            try:
                await fr.save_debug(page, debug_dir, index, attempt)
            except Exception as debug_exc:
                print(f"{prefix} could not save diagnostics: {debug_exc}", flush=True)
            if attempt > args.retries:
                break
            print(
                f"{prefix} attempt {attempt} failed: {fr.concise_error(exc)}; "
                "reloading Flow and retrying...",
                flush=True,
            )
            try:
                await fr.recover_flow_page(page)
            except Exception as recovery_exc:
                last_error = recovery_exc
                break

    if target is None:
        raise RuntimeError(
            f"video prompt {index} failed after {args.retries + 1} attempts: "
            f"{last_error}. Debug files: {debug_dir}"
        ) from last_error

    print(f"{prefix} saved {target}", flush=True)
    await page.wait_for_timeout(args.delay * 1000)
    return target


async def run(args: argparse.Namespace) -> int:
    project_dir = fr.resolve_project_dir(args.slug, args.project_dir)
    videos_dir = project_dir / "media" / "videos"
    prompt_file = Path(args.prompt_file).expanduser() if args.prompt_file else videos_dir / "prompts.plain.txt"
    debug_dir = project_dir / "media" / "flow-video-debug"
    videos_dir.mkdir(parents=True, exist_ok=True)
    prompts = fr.read_prompts(prompt_file)
    print(f"[flow] project: {project_dir}")
    print(f"[flow] videos:  {videos_dir}")
    print(f"[flow] prompts: {prompt_file} ({len(prompts)} total)")
    if args.dry_run:
        for index, prompt in enumerate(prompts, start=1):
            print(f"{index:03d}: {prompt}")
        return 0

    from playwright.async_api import async_playwright

    profile_dir = Path(args.profile_dir).expanduser()
    browser = fr.resolve_browser()
    print(f"[flow] browser: {browser['label']}")
    cdp_url = getattr(args, "cdp_url", None)

    async with async_playwright() as playwright:
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
            launch_kwargs["no_viewport"] = True
            browser_args.extend(["--start-maximized", "--window-position=0,0"])
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
                attached_browser = await playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
                attached_contexts = attached_browser.contexts
                if not attached_contexts:
                    raise RuntimeError("persistent Flow browser has no browser context")
                context = attached_contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(90_000)
                page.set_default_navigation_timeout(90_000)
                pages.append(page)
                capture = VideoCapture()
                capture.attach(page)
                captures.append(capture)
            else:
                this_profile = profile_dir
                if getattr(args, "profile_account", None):
                    slots = fr.expand_worker_slots(account_selector=args.profile_account)
                    if not slots:
                        raise RuntimeError(f"no enabled Flow account matches {args.profile_account!r}")
                    this_profile = fr.materialize_worker_profile(slots[0])
                this_profile.mkdir(parents=True, exist_ok=True)
                print(f"[flow] profile: {this_profile}", flush=True)
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
                    await fr.ensure_window_onscreen(page)
                last_nav_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        await page.goto(fr.FLOW_URL, wait_until="domcontentloaded", timeout=90_000)
                        last_nav_error = None
                        break
                    except Exception as exc:
                        last_nav_error = exc
                        print(f"[flow] goto attempt {attempt}/3 failed: {exc}", flush=True)
                        await page.wait_for_timeout(2_000 * attempt)
                if last_nav_error is not None:
                    raise last_nav_error
                await page.wait_for_timeout(3000)
                capture = VideoCapture()
                capture.attach(page)
                captures.append(capture)

            if not args.headless:
                print("[flow] Complete Google login if needed, then press Enter here.")
                input()
            for worker_number, page in enumerate(pages, start=1):
                print(f"[flow] worker {worker_number}: ensuring Flow editor is ready…", flush=True)
                await fr.ensure_not_blocked(page)
                await ensure_video_editor_ready(page, timeout_s=90.0)

            failures: list[Exception] = []
            saved: list[Path] = []
            for index, prompt in enumerate(prompts, start=1):
                try:
                    saved.append(
                        await process_video_prompt(
                            worker_number=1,
                            page=pages[0],
                            capture=captures[0],
                            index=index,
                            prompt=prompt,
                            prompt_count=len(prompts),
                            args=args,
                            videos_dir=videos_dir,
                            debug_dir=debug_dir,
                            project_dir=project_dir,
                        )
                    )
                except Exception as exc:
                    failures.append(exc)
                    break
            if failures:
                raise failures[0]
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
        default=str(fr.ROOT / ".flow" / "chromium-profile"),
        help="persistent browser profile; contains cookies, keep it private",
    )
    parser.add_argument("--profile-account")
    parser.add_argument(
        "--cdp-url",
        help="attach to an already-running persistent Chromium (for server-side Flow sessions)",
    )
    parser.add_argument("--headless", action="store_true", help="use only after headed login works")
    parser.add_argument("--timeout", type=int, default=1500, help="generation timeout per prompt in seconds")
    parser.add_argument("--delay", type=int, default=5, help="delay between prompts in seconds")
    parser.add_argument("--retries", type=int, default=2, help="retries per prompt after transient Flow errors")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.dry_run:
            return asyncio.run(run(args))
        project_dir = fr.resolve_project_dir(args.slug, args.project_dir)
        with fr.exclusive_flow_browser():
            with fr.exclusive_project_run(project_dir):
                return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[flow] stopped", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[flow] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())