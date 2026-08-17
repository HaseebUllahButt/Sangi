#!/usr/bin/env python3
"""Generate one video through the bundled Google Flow browser automation.

The WhatsApp agent calls this script after composing the final video prompt.
Like generate_image.py it drives the persistent authenticated Chromium session,
but it requests Video/Veo mode and saves mp4/webm output into workspace/ so the
bot can deliver it. The picture pipeline (flow_runner.py) is untouched.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
AGENT_ROOT = ROOT.parent
WORKSPACE = AGENT_ROOT / "workspace"
JOBS = ROOT / "jobs"
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}


def use_project_venv() -> None:
    """Run Flow with the dependencies bundled beside this script."""
    venv_root = ROOT / ".venv"
    venv_python = venv_root / "bin" / "python"
    if not venv_python.is_file():
        return
    if Path(sys.prefix).resolve() == venv_root.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])


def output_path(raw: str | None) -> Path:
    path = (WORKSPACE / f"video-{int(time.time())}.mp4") if not raw else Path(raw).expanduser()
    if not path.is_absolute():
        path = AGENT_ROOT / path
    path = path.resolve()
    try:
        path.relative_to(WORKSPACE.resolve())
    except ValueError as exc:
        raise ValueError("output must be inside whatsapp-agent/workspace") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output")
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
        help="persistent Google/Flow Chromium profile",
    )
    parser.add_argument(
        "--account",
        help="optional enabled Flow account directory or email from .flow/profiles.json",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show Chromium for first-run login or manual recovery",
    )
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("FLOW_CDP_URL", "http://127.0.0.1:9222"),
        help="persistent Chromium CDP endpoint used by non-headed runs",
    )
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()

    target = output_path(args.output)
    job_id = f"wa-video-{int(time.time())}-{target.stem}"
    job_dir = JOBS / job_id
    videos_dir = job_dir / "media" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = videos_dir / "prompts.plain.txt"
    prompt_file.write_text(args.prompt.strip() + "\n", encoding="utf-8")

    command = [
        sys.executable,
        str(ROOT / "flow_video_runner.py"),
        "--slug",
        job_id,
        "--project-dir",
        str(job_dir),
        "--prompt-file",
        str(prompt_file),
        "--profile-dir",
        str(Path(args.profile_dir).expanduser()),
        "--timeout",
        str(args.timeout),
    ]
    if not args.headed:
        command.extend(["--headless", "--cdp-url", args.cdp_url])
    if args.account:
        command.extend(["--profile-account", args.account])
    for reference in args.references:
        reference_path = Path(reference).expanduser().resolve()
        if not reference_path.is_file():
            print(f"[video-gen] reference image not found: {reference_path}", file=sys.stderr)
            return 2
        command.extend(["--reference", str(reference_path)])

    print(f"[video-gen] generating {target.name}", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        return result.returncode

    generated = sorted(
        p for p in videos_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not generated:
        print("[video-gen] Flow completed without a video", file=sys.stderr)
        return 1
    source = generated[-1]
    if source.suffix.lower() != target.suffix.lower() and target.suffix.lower() == ".mp4":
        converter = shutil.which("ffmpeg")
        if converter:
            ffmpeg_out = target.with_suffix(".ffmpeg.mp4")
            subprocess.run(
                [converter, "-y", "-i", str(source), "-c", "copy", str(ffmpeg_out)],
                check=False,
            )
            if ffmpeg_out.is_file():
                shutil.copy2(ffmpeg_out, target)
                ffmpeg_out.unlink()
            else:
                shutil.copy2(source, target)
        else:
            # Keep the original bytes if ffmpeg is unavailable; Flow's magic
            # bytes remain valid even when the requested suffix differs.
            shutil.copy2(source, target)
    else:
        shutil.copy2(source, target)
    print(f"VIDEO_GENERATED: {target}", flush=True)
    return 0


if __name__ == "__main__":
    use_project_venv()
    raise SystemExit(main())