"""Portable library paths, filenames, and artifact metadata."""

import hashlib
import html
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from .models import FileItem, MoodleError


def _clean_name(value: str, fallback: str = "item") -> str:
    value = html.unescape(value).strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def _activity_directory_name(
    section_number: int | None,
    activity_position: int | None,
    activity_id: int | None,
    *,
    fallback: str = "",
) -> str:
    """Return the sortable, title-independent directory for an LMS activity."""
    section = max(int(section_number or 0), 0)
    position = max(int(activity_position or 0), 0)
    if activity_id is None:
        digest = hashlib.sha1(fallback.encode("utf-8")).hexdigest()[:8]
        resolved_id = int(digest, 16)
    else:
        resolved_id = max(int(activity_id), 0)
    return f"{section:03d}--{position:03d}--{resolved_id}"


def _course_content_root(library: Path, course_id: int) -> Path:
    return library / "courses" / _clean_name(str(course_id), "course") / "content"


def _activity_root(library: Path, item: FileItem) -> Path:
    key = _activity_directory_name(
        item.section_number,
        item.activity_position,
        item.activity_id,
        fallback=f"{item.course_id}\0{item.url}\0{item.activity}\0{item.filename}",
    )
    return _course_content_root(library, item.course_id) / key


def _relative_browser_path(path: Path, library: Path) -> str:
    try:
        relative = path.resolve().relative_to(library.resolve())
    except ValueError as exc:
        raise MoodleError(f"library file is outside the configured library root: {path}") from exc
    return "/".join(urllib.parse.quote(part) for part in relative.parts)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _discover_artifacts(activity_directory: Path, library: Path) -> dict[str, dict[str, Any]]:
    artifacts_directory = activity_directory / "artifacts"
    definitions = {
        "transcript": ("transcript.fa.txt", "text", "fa"),
        "transcript_metadata": ("transcript.meta.json", "json", None),
        "summary": ("summary.fa.md", "markdown", "fa"),
        "summary_metadata": ("summary.meta.json", "json", None),
        "questions": ("questions.fa.md", "markdown", "fa"),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for kind, (filename, artifact_format, language) in definitions.items():
        path = artifacts_directory / filename
        if not path.is_file():
            continue
        record: dict[str, Any] = {
            "path": path.relative_to(activity_directory).as_posix(),
            "url": _relative_browser_path(path, library),
            "format": artifact_format,
            "size": path.stat().st_size,
        }
        if language:
            record["language"] = language
        artifacts[kind] = record
    return artifacts


def _filename_from_headers(headers: Any, url: str, fallback: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    star = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.I)
    normal = re.search(r'filename="?([^";]+)', disposition, flags=re.I)
    if star:
        name = urllib.parse.unquote(star.group(1))
    elif normal:
        name = normal.group(1).strip()
    else:
        name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    if not name:
        content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
        extension = mimetypes.guess_extension(content_type) or ""
        name = fallback + extension
    return _clean_name(name, fallback)


def _unique(items: Iterable[FileItem]) -> list[FileItem]:
    result: list[FileItem] = []
    seen: set[str] = set()
    for item in items:
        key = urllib.parse.urlsplit(item.url)._replace(query="", fragment="").geturl()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _is_cloudflare_challenge(page: str) -> bool:
    sample = page[:20000].casefold()
    return any(
        marker in sample
        for marker in (
            "/cdn-cgi/",
            "cf-browser-verification",
            "challenge-platform",
            "enable javascript and cookies to continue",
        )
    )
