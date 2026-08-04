"""One-time migration from the generated ``library/`` layout to ``public/``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import MoodleError
from .paths import _read_json_object


def migrate_library_to_public(legacy: Path, public: Path) -> dict[str, Any]:
    """Move private course bundles and create manifests before removing legacy metadata."""
    catalog_path = legacy / "courses.json"
    catalog = _read_json_object(catalog_path)
    courses = catalog.get("courses")
    if not isinstance(courses, list) or not courses:
        raise MoodleError(f"legacy course catalog not found: {catalog_path}")

    moved: list[dict[str, Any]] = []
    for course in courses:
        course_id = int(course.get("id") or 0)
        source_course = legacy / "courses" / str(course_id)
        target_course = public / "courses" / str(course_id)
        source_content = source_course / "content"
        target_content = target_course / "content"
        if target_content.exists():
            raise MoodleError(f"migration target already exists: {target_content}")
        if not source_content.is_dir():
            raise MoodleError(f"legacy course content not found: {source_content}")
        target_course.mkdir(parents=True, exist_ok=True)
        source_content.replace(target_content)
        migration_journal = source_course / "migration.json"
        if migration_journal.is_file():
            migration_journal.replace(target_course / "migration.json")
        moved.append({"id": course_id, "content": target_content.as_posix()})

    from .scan import _write_manifests

    result = _write_manifests(public, courses, remote=False)
    for course in courses:
        course_id = int(course.get("id") or 0)
        manifest = public / "courses" / str(course_id) / "manifest.json"
        if not manifest.is_file():
            raise MoodleError(f"migration did not create {manifest}")
        for item_metadata in (public / "courses" / str(course_id) / "content").rglob("item.json"):
            item_metadata.unlink()
        source_course = legacy / "courses" / str(course_id)
        (source_course / "course.json").unlink(missing_ok=True)
        try:
            source_course.rmdir()
        except OSError:
            pass

    for generated in ("app.js", "course.html", "index.html", "nginx-server.conf", "styles.css", "courses.json"):
        (legacy / generated).unlink(missing_ok=True)
    try:
        (legacy / "courses").rmdir()
        legacy.rmdir()
    except OSError:
        pass
    return {"catalog": result, "moved": moved}
