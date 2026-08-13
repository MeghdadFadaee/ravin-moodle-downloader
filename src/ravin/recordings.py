"""Import local recordings for Moodle live-class activities."""

from __future__ import annotations

import mimetypes
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from .constants import LIVE_CLASS_MODULES
from .local_files import archive_existing_file, atomic_copy
from .models import MoodleError
from .paths import _activity_directory_name, _clean_name, _read_json_object
from .scan import _atomic_json, _manifest_lock, scan_offline
from .wizard import prompt, prompt_path, select_number


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


@dataclass(frozen=True)
class RecordingImportResult:
    course_id: int
    activity_id: int
    activity_key: str
    activity_title: str
    recording: str
    archived: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["archived"] = list(self.archived)
        return value


def _is_live_activity(item: dict[str, Any]) -> bool:
    return (
        str(item.get("activity_type") or "").casefold() in LIVE_CLASS_MODULES
        or str(item.get("kind") or "").casefold() == "live class"
    )


def _recording_choices(course: dict[str, Any]) -> list[tuple[int, str, str, str, str]]:
    choices: list[tuple[int, str, str, str, str]] = []
    for section in course.get("sections", []):
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("name") or f"Section {section.get('number', '')}")
        for item in section.get("items", []):
            if not isinstance(item, dict) or not _is_live_activity(item):
                continue
            activity_id = int(item.get("activity_id") or 0)
            if not activity_id:
                continue
            title = str(item.get("title") or f"Live class {activity_id}")
            recording = str(item.get("filename") or "missing")
            transcript = str(item.get("state", {}).get("transcript") or "not available")
            choices.append((activity_id, title, section_title, recording, transcript))
    return choices


def _course_choices(public: Path) -> list[tuple[int, str, dict[str, Any]]]:
    choices: list[tuple[int, str, dict[str, Any]]] = []
    for manifest_path in sorted((public / "courses").glob("*/manifest.json")):
        manifest = _read_json_object(manifest_path)
        course = manifest.get("course")
        if not isinstance(course, dict) or not _recording_choices(course):
            continue
        course_id = int(course.get("id") or 0)
        if course_id:
            title = str(course.get("fullname") or course.get("shortname") or f"Course {course_id}")
            choices.append((course_id, title, course))
    choices.sort(key=lambda value: value[0])
    return choices


def _recording_source(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise MoodleError(f"recording file not found: {source}")
    mimetype = mimetypes.guess_type(source.name)[0] or ""
    if source.suffix.casefold() not in VIDEO_EXTENSIONS and not mimetype.startswith("video/"):
        formats = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise MoodleError(f"recording must be a video file ({formats})")
    if source.stat().st_size == 0:
        raise MoodleError("recording file is empty")
    return source


def _find_live_activity(
    course: dict[str, Any],
    activity_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for section in course.get("sections", []):
        if not isinstance(section, dict):
            continue
        for item in section.get("items", []):
            if isinstance(item, dict) and int(item.get("activity_id") or 0) == activity_id:
                matches.append((section, item))
    if not matches:
        raise MoodleError(f"activity {activity_id} was not found in course {course.get('id')}; run `ravin scan` first")
    if len(matches) > 1:
        raise MoodleError(f"activity {activity_id} appears more than once in the course manifest")
    section, item = matches[0]
    if not _is_live_activity(item):
        raise MoodleError(f"activity {activity_id} is not a live class")
    return section, item


def recording_wizard(
    public: Path,
    course_id: int | None = None,
    activity_id: int | None = None,
    recording_path: Path | None = None,
    *,
    input_func: Callable[[str], str] = input,
    output: TextIO | None = None,
) -> tuple[int, int, Path]:
    """Interactively fill omitted recording-import arguments."""
    public = public.expanduser().resolve()
    output = output or sys.stderr
    courses = _course_choices(public)
    if not courses:
        raise MoodleError(f"no local courses with live classes found in {public / 'courses'}; run `ravin scan` first")

    courses_by_id = {choice[0]: choice for choice in courses}
    if course_id is None:
        print("\nCourses with live classes:", file=output)
        for index, (candidate_id, title, course) in enumerate(courses, start=1):
            count = len(_recording_choices(course))
            print(f"  [{index}] {title} (course {candidate_id}, {count} live class(es))", file=output)
        course_id = select_number(courses, "Select a course: ", input_func, output)
    if course_id not in courses_by_id:
        raise MoodleError(f"course {course_id} has no local live classes; run `ravin scan {course_id}` first")

    course = courses_by_id[course_id][2]
    activities = _recording_choices(course)
    activity_ids = {choice[0] for choice in activities}
    if activity_id is None:
        print("\nLive classes:", file=output)
        for index, (candidate_id, title, section, recording, transcript) in enumerate(activities, start=1):
            print(
                f"  [{index}] {title} (activity {candidate_id}, section: {section}, "
                f"recording: {recording}, transcript: {transcript})",
                file=output,
            )
        activity_id = select_number(activities, "Select a live class: ", input_func, output)
    if activity_id not in activity_ids:
        raise MoodleError(f"activity {activity_id} is not a live class in course {course_id}")

    while recording_path is None:
        value = prompt(input_func, "Video recording file: ")
        try:
            recording_path = _recording_source(prompt_path(value))
        except MoodleError as exc:
            print(f"Invalid recording file: {exc}", file=output)
    return course_id, activity_id, _recording_source(recording_path)


def import_recording(
    public: Path,
    course_id: int,
    activity_id: int,
    recording_path: Path,
) -> RecordingImportResult:
    """Install a live-class recording and reconcile it as normal course media."""
    public = public.expanduser().resolve()
    source = _recording_source(recording_path)
    manifest_path = public / "courses" / str(course_id) / "manifest.json"
    manifest = _read_json_object(manifest_path)
    course = manifest.get("course")
    if not isinstance(course, dict):
        raise MoodleError(f"course manifest not found: {manifest_path}; run `ravin scan {course_id}` first")
    section, item = _find_live_activity(course, activity_id)
    key = str(item.get("key") or "") or _activity_directory_name(
        item.get("section_number", section.get("number")),
        item.get("activity_position"),
        activity_id,
    )
    activity_directory = public / "courses" / str(course_id) / "content" / key
    filename = _clean_name(source.name, "recording.mp4")
    destination = activity_directory / "files" / filename
    archived_paths: list[Path] = []
    try:
        if destination.is_file() and source.resolve() != destination.resolve():
            archived_paths.append(archive_existing_file(destination))
        atomic_copy(source, destination)
    except OSError as exc:
        raise MoodleError(f"could not import recording: {exc}") from exc

    destination_stat = destination.stat()
    _atomic_json(
        activity_directory / "artifacts" / "recording.meta.json",
        {
            "schema_version": 1,
            "source": f"../files/{filename}",
            "source_size": destination_stat.st_size,
            "source_mtime_ns": destination_stat.st_mtime_ns,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    mimetype = mimetypes.guess_type(filename)[0] or "video/mp4"

    with _manifest_lock(public / "courses"):
        current = _read_json_object(manifest_path)
        current_course = current.get("course")
        if not isinstance(current_course, dict):
            raise MoodleError(f"course manifest changed while importing recording: {manifest_path}")
        _current_section, current_item = _find_live_activity(current_course, activity_id)
        current_item.update(
            {
                "key": key,
                "bundle_path": f"content/{key}",
                "filename": filename,
                "extension": destination.suffix.removeprefix(".").casefold(),
                "kind": "video",
                "mimetype": mimetype,
            }
        )
        current_item.pop("remote_size", None)
        current["course"] = current_course
        _atomic_json(manifest_path, current)

    scan_offline(public, [course_id])
    return RecordingImportResult(
        course_id=course_id,
        activity_id=activity_id,
        activity_key=key,
        activity_title=str(item.get("title") or f"Activity {activity_id}"),
        recording=destination.relative_to(public).as_posix(),
        archived=tuple(path.relative_to(public).as_posix() for path in archived_paths),
    )


def format_recording_result(result: RecordingImportResult) -> str:
    archived = " The previous same-name recording was archived." if result.archived else ""
    return (
        f"Added recording for {result.activity_title} "
        f"(course {result.course_id}, activity {result.activity_id}).{archived}"
    )
