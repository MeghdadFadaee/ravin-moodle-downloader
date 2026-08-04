"""Migrate legacy course files into ordered activity bundles."""

import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .constants import CONTENT_SCHEMA_VERSION
from .models import FileItem, MoodleError
from .paths import _activity_root, _course_content_root, _read_json_object, _write_item_metadata


def _move_migration_file(source: Path, destination: Path, moved: list[dict[str, str]], source_root: Path, library: Path) -> None:
    if not source.is_file():
        if destination.is_file():
            return
        raise MoodleError(f"migration source is missing: {source}")
    if destination.exists():
        raise MoodleError(f"migration target already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    moved.append(
        {
            "from": source.relative_to(source_root).as_posix(),
            "to": destination.relative_to(library).as_posix(),
        }
    )


def _normalize_transcript_metadata(path: Path, source_filename: str) -> None:
    metadata = _read_json_object(path)
    if not metadata:
        return
    metadata["schema_version"] = CONTENT_SCHEMA_VERSION
    metadata["source"] = f"../files/{source_filename}"
    metadata["transcript"] = "transcript.fa.txt"
    if "transcribed_at" in metadata and "generated_at" not in metadata:
        metadata["generated_at"] = metadata.pop("transcribed_at")
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_summary_source(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    updated = re.sub(r"(?m)^\*\*منبع:\*\*.*$", "**منبع:** transcript.fa.txt", content)
    if updated != content:
        path.write_text(updated, encoding="utf-8")


def _finalize_item_metadata(activity_directory: Path) -> None:
    path = activity_directory / "item.json"
    metadata = _read_json_object(path)
    artifact_names = {
        "transcript": "transcript.fa.txt",
        "transcript_metadata": "transcript.meta.json",
        "summary": "summary.fa.md",
        "questions": "questions.fa.md",
    }
    artifacts = {
        kind: f"artifacts/{filename}"
        for kind, filename in artifact_names.items()
        if (activity_directory / "artifacts" / filename).is_file()
    }
    metadata["artifacts"] = artifacts
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_file_item(course_id: int, section: dict[str, Any], record: dict[str, Any], filename: str) -> FileItem:
    return FileItem(
        course_id=course_id,
        section=str(section.get("name") or "Course"),
        activity=str(record.get("title") or filename),
        filename=filename,
        url=str(record.get("source_url") or ""),
        mimetype=str(record.get("mimetype") or mimetypes.guess_type(filename)[0] or ""),
        filesize=record.get("size"),
        chapter=str(section.get("name") or "Course"),
        section_id=record.get("section_id", section.get("id")),
        section_number=record.get("section_number", section.get("number")),
        activity_id=record.get("activity_id"),
        activity_type=str(record.get("activity_type") or "resource"),
        activity_position=record.get("activity_position"),
        description=str(record.get("description") or ""),
    )


def _migrate_legacy_downloads(source_root: Path, library: Path, selected_course_ids: Iterable[int] = ()) -> dict[str, Any]:
    """Move the old flat download layout into ordered activity bundles."""
    catalog_path = library / "courses.json"
    if not catalog_path.is_file():
        raise MoodleError(f"{catalog_path} is required to map legacy files to LMS activities")
    catalog = _read_json_object(catalog_path)
    selected = set(selected_course_ids)
    courses = [
        course
        for course in catalog.get("courses", [])
        if not selected or int(course.get("id") or 0) in selected
    ]
    if selected - {int(course.get("id") or 0) for course in courses}:
        missing = ", ".join(str(value) for value in sorted(selected - {int(course.get("id") or 0) for course in courses}))
        raise MoodleError(f"course ID(s) are missing from {catalog_path}: {missing}")

    migration: dict[str, Any] = {
        "schema_version": CONTENT_SCHEMA_VERSION,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "courses": [],
    }
    for course in courses:
        course_id = int(course.get("id") or 0)
        legacy_course = source_root / str(course_id)
        if not legacy_course.is_dir():
            continue
        moved: list[dict[str, str]] = []
        records: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (section, record)
            for section in course.get("sections", [])
            for record in section.get("items", [])
        ]
        records_by_filename = {
            str(record.get("filename")): (section, record)
            for section, record in records
            if record.get("filename")
        }
        files_directory = legacy_course / "Course files"
        for filename, (section, record) in records_by_filename.items():
            source_file = files_directory / filename
            if not source_file.is_file():
                continue
            item = _record_file_item(course_id, section, record, filename)
            activity_directory = _activity_root(library, item)
            destination = activity_directory / "files" / filename
            _move_migration_file(source_file, destination, moved, source_root, library)
            _write_item_metadata(item, activity_directory, destination)

            companions = (
                (files_directory / f"{filename}.txt", activity_directory / "artifacts" / "transcript.fa.txt"),
                (files_directory / f"{filename}.txt.json", activity_directory / "artifacts" / "transcript.meta.json"),
                (files_directory / f"{filename}.md", activity_directory / "artifacts" / "summary.fa.md"),
            )
            for companion_source, companion_destination in companions:
                if companion_source.is_file():
                    _move_migration_file(companion_source, companion_destination, moved, source_root, library)
            metadata_path = activity_directory / "artifacts" / "transcript.meta.json"
            if metadata_path.is_file():
                _normalize_transcript_metadata(metadata_path, filename)
            summary_path = activity_directory / "artifacts" / "summary.fa.md"
            if summary_path.is_file():
                _normalize_summary_source(summary_path)
            _finalize_item_metadata(activity_directory)

        quizzes = sorted(
            ((section, record) for section, record in records if record.get("activity_type") == "quiz"),
            key=lambda value: (
                int(value[1].get("section_number", value[0].get("number")) or 0),
                int(value[1].get("activity_position") or 0),
            ),
        )
        exams_directory = legacy_course / "Course exams"
        exam_pdfs = sorted(exams_directory.glob("*.pdf")) if exams_directory.is_dir() else []
        if len(exam_pdfs) > len(quizzes):
            raise MoodleError(f"course {course_id} has more local exams than LMS quiz activities")
        for exam_index, exam_pdf in enumerate(exam_pdfs):
            section, record = quizzes[exam_index]
            item = _record_file_item(course_id, section, record, exam_pdf.name)
            activity_directory = _activity_root(library, item)
            destination = activity_directory / "files" / exam_pdf.name
            _move_migration_file(exam_pdf, destination, moved, source_root, library)
            _write_item_metadata(item, activity_directory, destination)
            questions = exam_pdf.with_suffix(".md")
            if questions.is_file():
                _move_migration_file(
                    questions,
                    activity_directory / "artifacts" / "questions.fa.md",
                    moved,
                    source_root,
                    library,
                )
            _finalize_item_metadata(activity_directory)

        course_root = library / "courses" / str(course_id)
        course_root.mkdir(parents=True, exist_ok=True)
        course_migration = {
            "course_id": course_id,
            "source_layout": "downloads/<course-id>/{Course files,Course exams}",
            "target_layout": "library/courses/<course-id>/content/SECTION--POSITION--ACTIVITY",
            "moved": moved,
        }
        (course_root / "migration.json").write_text(
            json.dumps(course_migration, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        migration["courses"].append({"id": course_id, "moved_files": len(moved)})

        remaining = [
            path
            for path in legacy_course.rglob("*")
            if path.is_file() and path.name not in {".DS_Store", "files-map.txt"}
        ]
        if remaining:
            names = ", ".join(str(path.relative_to(legacy_course)) for path in remaining[:5])
            raise MoodleError(f"migration left unclassified files in course {course_id}: {names}")
        for disposable in legacy_course.rglob(".DS_Store"):
            disposable.unlink(missing_ok=True)
        (legacy_course / "files-map.txt").unlink(missing_ok=True)
        for directory in sorted((path for path in legacy_course.rglob("*") if path.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            legacy_course.rmdir()
        except OSError:
            pass
    return migration
