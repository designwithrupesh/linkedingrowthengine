"""Load checked, authored media without rebuilding it as a template.

The registry binds the reviewed copy to exact local bytes. It grants no write
permission and never generates a replacement when a reference is invalid.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from PIL import Image

from automation import voice
from automation.media_posts import validate_media_asset
from automation.visual_assets import VisualAssetError


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "automation" / "art-directed-content.json"
PUBLIC_RESOURCE_BASE = (
    "https://raw.githubusercontent.com/designwithrupesh/linkedingrowthengine/"
    "main/resources/"
)
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_FIELDS = {
    "id", "kind", "title", "caption", "file", "preview", "sha256",
    "page_count", "transcript", "design_brief", "review_status",
}
_BRIEF_FIELDS = {
    "reader_tension", "intended_feeling", "core_decision", "visual_idea",
    "story_arc", "tradeoff",
}


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise VisualAssetError("Authored media contains missing or oversized copy")
    return value


def _ground_text(value: Any, policy: dict[str, Any]) -> None:
    """Audit every copy field, including the rationale supplied for review."""
    from automation.twin import validate_grounding

    if isinstance(value, str):
        voice.require_plain_text(value)
        if validate_grounding({"text": value, "skip": False, "reason": ""}, policy)["skip"]:
            raise VisualAssetError("Authored media claimed unsupported personal experience")
    elif isinstance(value, list):
        for item in value:
            _ground_text(item, policy)
    elif isinstance(value, dict):
        for item in value.values():
            _ground_text(item, policy)


def _resource_path(value: Any, *, suffix: str) -> Path:
    """Resolve only existing files physically contained by resources/."""
    if (not isinstance(value, str) or not value or "\\" in value
            or len(value) > 500 or "\x00" in value):
        raise VisualAssetError("Authored media path must name a local resource")
    relative = PurePosixPath(value)
    if (relative.is_absolute() or ".." in relative.parts or "." in value.split("/")
            or not relative.parts or relative.parts[0] != "resources"
            or relative.suffix != suffix or ":" in value):
        raise VisualAssetError("Authored media path must remain below resources")
    try:
        resources = (ROOT / "resources").resolve(strict=True)
        resources.relative_to(ROOT.resolve())
        resolved = (ROOT / relative).resolve(strict=True)
        resolved.relative_to(resources)
    except (OSError, ValueError, RuntimeError):
        raise VisualAssetError("Authored media resource is missing or escapes resources") from None
    if not resolved.is_file():
        raise VisualAssetError("Authored media resource must be a regular file")
    return resolved


def _preview(path: Path) -> None:
    """Check the preview is a real, bounded PNG, not an arbitrary path."""
    data = path.read_bytes()
    if not data or len(data) > 50 * 1024 * 1024:
        raise VisualAssetError("Authored media preview exceeds supported bounds")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if (image.format != "PNG" or width <= 0 or height <= 0
                    or width * height > 36152320):
                raise ValueError
            image.verify()
    except Exception:
        raise VisualAssetError("Authored media preview must be a valid PNG") from None


def _pdf_pages(path: Path, expected: int) -> None:
    """Read the actual page tree before accepting the registry's page count."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or len(reader.pages) != expected:
            raise ValueError
        # Access each page so a malformed leaf cannot hide behind /Count.
        for page in reader.pages:
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise ValueError
    except Exception:
        raise VisualAssetError("Authored PDF is invalid or its page count does not match") from None


def _entry(reference: Any, kind: str) -> dict[str, Any]:
    if (not isinstance(reference, str) or len(reference) > 80
            or not _ID.fullmatch(reference)):
        raise VisualAssetError("Authored media reference must be a valid id")
    if kind not in {"image", "carousel", "freebie"}:
        raise VisualAssetError("Authored media requires an image or PDF format")
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VisualAssetError("Authored media registry could not be read") from None
    if (not isinstance(registry, dict) or set(registry) != {"version", "assets"}
            or type(registry["version"]) is not int or registry["version"] != 1
            or not isinstance(registry["assets"], list)):
        raise VisualAssetError("Authored media registry has an unsupported structure")
    matches = [item for item in registry["assets"]
               if isinstance(item, dict) and item.get("id") == reference]
    if len(matches) != 1 or matches[0].get("kind") != kind:
        raise VisualAssetError("Authored media reference is missing, ambiguous or has a different format")
    entry = deepcopy(matches[0])
    if set(entry) != _FIELDS or entry.get("review_status") != "checked":
        raise VisualAssetError("Authored media must have complete checked provenance")
    _text(entry["title"], 180)
    _text(entry["caption"], 3000)
    transcript = entry["transcript"]
    if not isinstance(transcript, list) or not 1 <= len(transcript) <= 300:
        raise VisualAssetError("Authored media must include its visible copy transcript")
    for item in transcript:
        _text(item, 3000)
    brief = entry["design_brief"]
    if not isinstance(brief, dict) or set(brief) != _BRIEF_FIELDS:
        raise VisualAssetError("Authored media must include its communication brief")
    for key, item in brief.items():
        if key == "story_arc":
            if not isinstance(item, list) or not 1 <= len(item) <= 30:
                raise VisualAssetError("Authored media must include its story sequence")
            for step in item:
                _text(step, 1000)
        else:
            _text(item, 1500)
    if not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
        raise VisualAssetError("Authored media must bind an exact SHA256 checksum")
    pages = entry["page_count"]
    if (type(pages) is not int or not 1 <= pages <= 300
            or (kind == "image" and pages != 1)):
        raise VisualAssetError("Authored media page count is invalid")
    return entry


def load_art_directed(reference: Any, kind: str,
                      policy: dict[str, Any]) -> dict[str, Any]:
    """Return the exact checked file; never render, generate or modify state."""
    entry = _entry(reference, kind)
    path = _resource_path(entry["file"], suffix=".png" if kind == "image" else ".pdf")
    preview = _resource_path(entry["preview"], suffix=".png")
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise VisualAssetError("Authored media bytes differ from the checked checksum")
    _preview(preview)
    download_url = None
    if kind == "freebie":
        relative = PurePosixPath(entry["file"]).relative_to("resources")
        download_url = PUBLIC_RESOURCE_BASE + relative.as_posix()
        if download_url not in entry["caption"]:
            entry["caption"] += "\n\nDownload the worksheet: " + download_url
    if len(entry["caption"]) > 3000:
        raise VisualAssetError("Authored media caption exceeds the publication limit")
    _ground_text({key: entry[key] for key in ("title", "caption", "transcript", "design_brief")}, policy)
    asset = {
        "id": entry["id"], "asset_id": entry["id"], "kind": kind,
        "title": entry["title"], "caption": entry["caption"],
        "path": str(path), "preview_path": str(preview),
        "mime_type": "image/png" if kind == "image" else "application/pdf",
        "sha256": entry["sha256"], "page_count": entry["page_count"],
        "download_url": download_url,
        "spec": {"source": "checked-art-directed", **entry},
    }
    try:
        validate_media_asset(asset)
    except (OSError, ValueError):
        raise VisualAssetError("Authored media does not contain a valid supported attachment") from None
    if kind != "image":
        _pdf_pages(path, entry["page_count"])
    return asset
