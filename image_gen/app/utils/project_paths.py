"""Canonical project path helpers for the CLI workflows."""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_ROOT = REPO_ROOT / "projects"

# Folders named like a slug but only holding library stills/clips, not a project.
_NON_PROJECT_PATH_MARKERS = (
    "/library/pics/",
    "/library/Vids/",
    "/library/vids/",
    "/media/images/",
    "/media/stock/",
    "/media/thumbnails/",
    "/media/library/",
    "/.edit-cache/",
)


def looks_like_project_dir(path: Path) -> bool:
    """True when ``path`` is a real long/short project root, not a pics dump."""
    if not path.is_dir():
        return False
    # Reject channel-library subfolders that re-use the slug as a directory name
    # (e.g. library/pics/10-pot-fruits after assemble ingest).
    norm = path.as_posix()
    if any(marker in norm for marker in _NON_PROJECT_PATH_MARKERS):
        return False
    markers = (
        path / "media" / "media_plan.json",
        path / "media" / "edit.json",
        path / "project.json",
        path / "script",
        path / "audio",
        path / "short",
        path / "media",
    )
    return any(m.exists() for m in markers)


def resolve_project_dir(slug: str, explicit: str | Path | None = None) -> Path:
    """Find a project by slug without flattening family/channel folders.

    Existing layouts such as ``projects/YtShorts/<slug>`` and
    ``projects/LongForms/BioLiving/<slug>`` are preferred over recreating the
    old flat ``projects/<slug>`` path. New projects still use the flat fallback
    unless the caller supplies ``--project-dir``.

    Name collisions with library ingest folders (``library/pics/<slug>``) are
    ignored so the editor can resolve media URLs without ``project_dir``.
    """
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    candidates: list[Path] = []
    flat = PROJECTS_ROOT / slug
    if flat.is_dir():
        candidates.append(flat)
    if PROJECTS_ROOT.is_dir():
        candidates.extend(
            path
            for path in PROJECTS_ROOT.rglob(slug)
            if path.is_dir() and path.name == slug and path not in candidates
        )

    projectish = [p for p in candidates if looks_like_project_dir(p)]
    if projectish:
        candidates = projectish

    if len(candidates) > 1:
        choices = ", ".join(str(path.relative_to(REPO_ROOT)) for path in candidates)
        raise ValueError(
            f"slug {slug!r} is ambiguous; choose one with --project-dir: {choices}"
        )
    return candidates[0] if candidates else flat
