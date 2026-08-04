"""Build, write, and refresh the static course catalog."""

import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from .client import MoodleClient
from .constants import LIBRARY_ASSETS
from .models import FileItem, MoodleError
from .paths import (
    _activity_directory_name, _activity_root, _clean_name, _course_content_root,
    _discover_artifacts, _read_json_object, _relative_browser_path,
)


def _format_size(size: int | None) -> str:
    if size is None:
        return "?"
    units = ["B", "KiB", "MiB", "GiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def _resource_kind(item: FileItem) -> str:
    mimetype = item.mimetype.casefold()
    suffix = Path(item.filename).suffix.casefold()
    if mimetype.startswith("video/") or suffix in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}:
        return "video"
    if mimetype.startswith("audio/") or suffix in {".mp3", ".m4a", ".wav", ".ogg", ".flac"}:
        return "audio"
    if (
        mimetype.startswith("text/")
        or mimetype in {"application/pdf", "application/msword"}
        or suffix in {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md"}
    ):
        return "document"
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return "archive"
    return "file"


def _local_resource(item: FileItem, library: Path) -> tuple[str, Path | None]:
    directory = _activity_root(library, item) / "files"
    expected = directory / _clean_name(item.filename, _clean_name(item.activity, "activity"))
    if expected.is_file():
        return "downloaded", expected
    partial = expected.with_name(expected.name + ".part")
    if partial.is_file():
        return "partial", partial
    if directory.is_dir():
        expected_name = expected.name.casefold()
        matches = [path for path in directory.iterdir() if path.is_file() and path.name.casefold() == expected_name]
        if len(matches) == 1:
            return "downloaded", matches[0]
    return "missing", None


def _local_activity_record(
    library: Path,
    course_id: int,
    section: dict[str, Any],
    activity: dict[str, Any],
) -> tuple[dict[str, Any], Path] | None:
    key = _activity_directory_name(
        section.get("number"),
        activity.get("position"),
        activity.get("id"),
        fallback=f"{course_id}\0{activity.get('url', '')}\0{activity.get('name', '')}",
    )
    directory = _course_content_root(library, course_id) / key
    metadata = _read_json_object(directory / "item.json")
    if not metadata:
        return None
    return metadata, directory


def _build_library_catalog(
    client: MoodleClient,
    library: Path,
    selected_course_ids: Iterable[int] = (),
) -> dict[str, Any]:
    selected_ids = set(selected_course_ids)
    available_courses = client.list_courses()
    courses = [course for course in available_courses if not selected_ids or course.id in selected_ids]
    missing_ids = selected_ids - {course.id for course in courses}
    if missing_ids:
        missing = ", ".join(str(course_id) for course_id in sorted(missing_ids))
        raise MoodleError(f"course ID(s) not found in your enrollments: {missing}")

    catalog_courses: list[dict[str, Any]] = []
    total_files = 0
    total_activities = 0
    total_records = 0
    total_downloaded = 0
    total_downloaded_bytes = 0
    for course in courses:
        print(f"Reading course {course.id}: {course.fullname}", file=sys.stderr)
        downloaded_count = 0
        downloaded_bytes = 0
        items = client.list_files(course.id)
        try:
            structure_method = getattr(client, "course_structure")
            structure = structure_method(course.id)
        except (AttributeError, MoodleError):
            structure = []
        consumed: set[int] = set()

        def file_record(
            item: FileItem,
            item_index: int,
            section_metadata: dict[str, Any] | None = None,
            activity_metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            nonlocal downloaded_count, downloaded_bytes
            section_metadata = section_metadata or {}
            activity_metadata = activity_metadata or {}
            status, local_path = _local_resource(item, library)
            local_bytes = local_path.stat().st_size if local_path else 0
            if status == "downloaded":
                downloaded_count += 1
                downloaded_bytes += local_bytes
            stable_source = urllib.parse.urlsplit(item.url)._replace(query="", fragment="").geturl()
            resource_id = hashlib.sha1(
                f"{course.id}\0{stable_source}\0{item.filename}".encode("utf-8")
            ).hexdigest()[:14]
            title = activity_metadata.get("name") or re.sub(r"\s*فایل\s*$", "", item.activity).strip()
            consumed.add(item_index)
            activity_directory = _activity_root(library, item)
            return {
                "id": resource_id,
                "section": item.section,
                "section_id": section_metadata.get("id", item.section_id),
                "section_number": section_metadata.get("number", item.section_number),
                "activity_id": activity_metadata.get("id", item.activity_id),
                "activity_position": activity_metadata.get("position", item.activity_position),
                "activity_type": activity_metadata.get("type", item.activity_type or "resource"),
                "title": title or item.filename,
                "description": activity_metadata.get("description") or item.description,
                "badge": activity_metadata.get("badge") or Path(item.filename).suffix.removeprefix(".").upper(),
                "filename": item.filename,
                "extension": Path(item.filename).suffix.removeprefix(".").casefold(),
                "kind": _resource_kind(item),
                "mimetype": item.mimetype,
                "status": status,
                "size": local_bytes if status == "downloaded" else item.filesize,
                "local_bytes": local_bytes,
                "local_url": _relative_browser_path(local_path, library) if status == "downloaded" and local_path else None,
                "artifacts": _discover_artifacts(activity_directory, library),
                "source_url": activity_metadata.get("url") or f"{client.site}/course/view.php?id={course.id}",
                "lms_completed": activity_metadata.get("lms_completed"),
            }

        files_by_activity: dict[int, list[tuple[int, FileItem]]] = {}
        for item_index, item in enumerate(items):
            if item.activity_id is not None:
                files_by_activity.setdefault(item.activity_id, []).append((item_index, item))

        catalog_sections: list[dict[str, Any]] = []
        activity_count = 0
        for section in structure:
            section_records: list[dict[str, Any]] = []
            activities = section.get("activities", [])
            activity_count += len(activities)
            for activity in activities:
                matches = files_by_activity.get(activity.get("id"), [])
                if not matches:
                    activity_name = re.sub(r"\s*فایل\s*$", "", str(activity.get("name") or "")).strip().casefold()
                    matches = [
                        (item_index, item)
                        for item_index, item in enumerate(items)
                        if item_index not in consumed
                        and re.sub(r"\s*فایل\s*$", "", item.activity).strip().casefold() == activity_name
                    ]
                if matches:
                    for item_index, item in matches:
                        if item_index not in consumed:
                            section_records.append(file_record(item, item_index, section, activity))
                    continue
                activity_type = str(activity.get("type") or "activity")
                type_names = {
                    "bigbluebuttonbn": "live class",
                    "forum": "discussion",
                    "quiz": "quiz",
                    "url": "link",
                    "resource": "resource",
                }
                local_activity = _local_activity_record(library, course.id, section, activity)
                local_metadata, local_directory = local_activity or ({}, Path())
                local_files = [
                    local_directory / str(relative)
                    for relative in local_metadata.get("files", [])
                    if local_directory and (local_directory / str(relative)).is_file()
                ]
                local_path = local_files[0] if local_files else None
                local_bytes = local_path.stat().st_size if local_path else 0
                local_kind = "assessment" if activity_type == "quiz" and local_activity else type_names.get(activity_type, "activity")
                if local_path:
                    downloaded_count += 1
                    downloaded_bytes += local_bytes
                section_records.append(
                    {
                        "id": f"activity-{activity.get('id') or hashlib.sha1(str(activity).encode()).hexdigest()[:10]}",
                        "section": section.get("name") or "Course",
                        "section_id": section.get("id"),
                        "section_number": section.get("number"),
                        "activity_id": activity.get("id"),
                        "activity_position": activity.get("position"),
                        "activity_type": activity_type,
                        "title": activity.get("name") or activity_type,
                        "description": activity.get("description") or "",
                        "badge": activity.get("badge") or type_names.get(activity_type, activity_type),
                        "filename": local_path.name if local_path else "",
                        "extension": local_path.suffix.removeprefix(".").casefold() if local_path else "",
                        "kind": local_kind,
                        "mimetype": mimetypes.guess_type(local_path.name)[0] if local_path else "",
                        "status": "downloaded" if local_path else "online",
                        "size": local_bytes if local_path else None,
                        "local_bytes": local_bytes,
                        "local_url": _relative_browser_path(local_path, library) if local_path else None,
                        "artifacts": _discover_artifacts(local_directory, library) if local_activity else {},
                        "source_url": activity.get("url") or f"{client.site}/course/view.php?id={course.id}",
                        "lms_completed": activity.get("lms_completed"),
                    }
                )
            catalog_sections.append(
                {
                    "id": section.get("id"),
                    "number": section.get("number"),
                    "position": section.get("position"),
                    "name": section.get("name") or "Section",
                    "summary": section.get("summary") or "",
                    "activity_count": len(activities),
                    "items": section_records,
                }
            )

        for item_index, item in enumerate(items):
            if item_index in consumed:
                continue
            chapter_name = item.chapter or item.section
            target = next((section for section in catalog_sections if section["name"] == chapter_name), None)
            if target is None:
                target = {
                    "id": item.section_id,
                    "number": item.section_number,
                    "position": len(catalog_sections) + 1,
                    "name": chapter_name,
                    "summary": "",
                    "activity_count": 0,
                    "items": [],
                }
                catalog_sections.append(target)
            target["items"].append(file_record(item, item_index))

        if not structure:
            activity_count = len(items)
            for section in catalog_sections:
                section["activity_count"] = len(section["items"])
        record_count = sum(len(section["items"]) for section in catalog_sections)
        file_count = sum(
            1
            for section in catalog_sections
            for record in section["items"]
            if record.get("filename")
        )
        type_counts: dict[str, int] = {}
        for section in catalog_sections:
            for record in section["items"]:
                type_counts[record["kind"]] = type_counts.get(record["kind"], 0) + 1
        catalog_courses.append(
            {
                "id": course.id,
                "fullname": course.fullname,
                "shortname": course.shortname,
                "source_url": f"{client.site}/course/view.php?id={course.id}",
                "section_count": len(catalog_sections),
                "activity_count": activity_count,
                "record_count": record_count,
                "file_count": file_count,
                "downloaded_count": downloaded_count,
                "downloaded_bytes": downloaded_bytes,
                "type_counts": type_counts,
                "sections": catalog_sections,
            }
        )
        total_files += file_count
        total_activities += activity_count
        total_records += record_count
        total_downloaded += downloaded_count
        total_downloaded_bytes += downloaded_bytes

    return {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "courses": len(catalog_courses),
            "files": total_files,
            "activities": total_activities,
            "records": total_records,
            "downloaded_files": total_downloaded,
            "downloaded_bytes": total_downloaded_bytes,
        },
        "courses": catalog_courses,
    }


def _write_library_site(library: Path, catalog: dict[str, Any]) -> Path:
    library.mkdir(parents=True, exist_ok=True)
    legacy_media_link = library / "media"
    if legacy_media_link.is_symlink():
        legacy_media_link.unlink()
    elif legacy_media_link.exists():
        raise MoodleError(f"{legacy_media_link} is not a symlink; move it aside before rebuilding")
    asset_root = resources.files("ravin.assets")
    for name in LIBRARY_ASSETS:
        (library / name).write_bytes(asset_root.joinpath(name).read_bytes())
    catalog_path = library / "courses.json"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for course in catalog.get("courses", []):
        course_id = int(course.get("id") or 0)
        course_root = library / "courses" / str(course_id)
        course_root.mkdir(parents=True, exist_ok=True)
        (course_root / "course.json").write_text(
            json.dumps(course, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    quoted_root = json.dumps(str(library.resolve()))
    (library / "nginx-server.conf").write_text(
        f"""# Generated by ravin. Include this file inside nginx's http block.
server {{
    listen 127.0.0.1:8765;
    server_name localhost;
    root {quoted_root};
    index index.html;
    charset utf-8;
    autoindex off;

    location = / {{
        try_files /index.html =404;
    }}

    location = /courses.json {{
        try_files $uri =404;
        default_type application/json;
        add_header Cache-Control "no-store" always;
    }}

    location /courses/ {{
        try_files $uri =404;
        sendfile on;
        tcp_nopush on;
        add_header Accept-Ranges bytes always;
        add_header X-Content-Type-Options nosniff always;
    }}

    location / {{
        try_files $uri $uri/ =404;
    }}

    location ~ (^|/)\\. {{ return 404; }}
    location ~ \\.part$ {{ return 404; }}
}}
""",
        encoding="utf-8",
    )
    return catalog_path


def _reuse_library_catalog(library: Path) -> dict[str, Any]:
    catalog_path = library / "courses.json"
    if not catalog_path.is_file():
        raise MoodleError(f"{catalog_path} does not exist; build the library online first")
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MoodleError(f"could not read the existing course catalog: {exc}") from exc
    catalog["schema_version"] = 3
    total_files = 0
    total_records = 0
    total_downloaded = 0
    total_downloaded_bytes = 0
    for course in catalog.get("courses", []):
        course_id = int(course.get("id") or 0)
        course_file_count = 0
        course_downloaded = 0
        course_downloaded_bytes = 0
        for section in course.get("sections", []):
            for item in section.get("items", []):
                item["title"] = html.unescape(str(item.get("title") or ""))
                item["description"] = html.unescape(str(item.get("description") or ""))
                key = _activity_directory_name(
                    item.get("section_number", section.get("number")),
                    item.get("activity_position"),
                    item.get("activity_id"),
                    fallback=f"{course_id}\0{item.get('source_url', '')}\0{item.get('title', '')}",
                )
                activity_directory = _course_content_root(library, course_id) / key
                metadata = _read_json_object(activity_directory / "item.json")
                local_files = [
                    activity_directory / str(relative)
                    for relative in metadata.get("files", [])
                    if (activity_directory / str(relative)).is_file()
                ]
                filename = str(item.get("filename") or "")
                if filename:
                    course_file_count += 1
                if not local_files and filename:
                    expected = activity_directory / "files" / filename
                    if expected.is_file():
                        local_files = [expected]
                local_path = next((path for path in local_files if not filename or path.name == filename), None)
                local_path = local_path or (local_files[0] if local_files else None)
                if local_path:
                    local_bytes = local_path.stat().st_size
                    if not filename:
                        course_file_count += 1
                    item.update(
                        {
                            "filename": local_path.name,
                            "extension": local_path.suffix.removeprefix(".").casefold(),
                            "status": "downloaded",
                            "size": local_bytes,
                            "local_bytes": local_bytes,
                            "local_url": _relative_browser_path(local_path, library),
                            "artifacts": _discover_artifacts(activity_directory, library),
                        }
                    )
                    if item.get("activity_type") == "quiz":
                        item["kind"] = "assessment"
                    course_downloaded += 1
                    course_downloaded_bytes += local_bytes
                elif item.get("status") == "downloaded":
                    item.update({"status": "missing", "local_bytes": 0, "local_url": None, "artifacts": {}})
        course["file_count"] = course_file_count
        course["downloaded_count"] = course_downloaded
        course["downloaded_bytes"] = course_downloaded_bytes
        course_records = [item for section in course.get("sections", []) for item in section.get("items", [])]
        type_counts: dict[str, int] = {}
        for item in course_records:
            kind = str(item.get("kind") or "activity")
            type_counts[kind] = type_counts.get(kind, 0) + 1
        course["record_count"] = len(course_records)
        course["type_counts"] = type_counts
        total_files += course_file_count
        total_records += len(course_records)
        total_downloaded += course_downloaded
        total_downloaded_bytes += course_downloaded_bytes
    stats = catalog.setdefault("stats", {})
    stats.update(
        {
            "files": total_files,
            "records": total_records,
            "downloaded_files": total_downloaded,
            "downloaded_bytes": total_downloaded_bytes,
        }
    )
    catalog["prepared_at"] = datetime.now(timezone.utc).isoformat()
    return catalog
