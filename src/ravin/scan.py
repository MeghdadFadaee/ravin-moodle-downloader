"""Remote course scanning, local state reconciliation, and manifest storage."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .catalog import _build_library_catalog
from .client import MoodleClient
from .models import FileItem, MoodleError
from .paths import (
    _activity_directory_name,
    _clean_name,
    _course_content_root,
    _discover_artifacts,
    _read_json_object,
    _relative_browser_path,
)


MANIFEST_SCHEMA_VERSION = 1
STATE_VALUES = {"missing", "partial", "complete", "stale", "error", "not_applicable"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _manifest_lock(courses_root: Path) -> Iterator[None]:
    courses_root.mkdir(parents=True, exist_ok=True)
    lock_path = courses_root / ".manifest.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _activity_directory(public: Path, course_id: int, section: dict[str, Any], item: dict[str, Any]) -> Path:
    key = _activity_directory_name(
        item.get("section_number", section.get("number")),
        item.get("activity_position"),
        item.get("activity_id"),
        fallback=f"{course_id}\0{item.get('source_url', '')}\0{item.get('title', '')}",
    )
    item["key"] = key
    item["bundle_path"] = f"content/{key}"
    return _course_content_root(public, course_id) / key


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary_metadata(summary: Path, transcript: Path) -> dict[str, Any]:
    transcript_stat = transcript.stat()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": "transcript.fa.txt",
        "source_sha256": _sha256(transcript),
        "source_size": transcript_stat.st_size,
        "source_mtime_ns": transcript_stat.st_mtime_ns,
        "generated_at": datetime.fromtimestamp(summary.stat().st_mtime, timezone.utc).isoformat(),
    }


def _artifact_state(
    activity_directory: Path,
    kind: str,
    applicable: bool,
    current_source: Path | None = None,
) -> str:
    artifacts = activity_directory / "artifacts"
    if kind == "transcript":
        transcript = artifacts / "transcript.fa.txt"
        failed = (artifacts / ".transcript.error.json").is_file()
        if not transcript.is_file():
            if not applicable:
                return "not_applicable"
            return "error" if failed else "missing"
        metadata = _read_json_object(artifacts / "transcript.meta.json")
        source_files: list[Path] = []
        source_name = metadata.get("source") if metadata else None
        if isinstance(source_name, str):
            candidate = (artifacts / source_name).resolve()
            try:
                candidate.relative_to(activity_directory.resolve())
            except ValueError:
                candidate = Path()
            if candidate.is_file():
                source_files.append(candidate)
        if not source_files:
            source_files = sorted(
                path
                for path in (activity_directory / "files").glob("*")
                if path.is_file() and not path.name.endswith(".part")
            )
        if not metadata or not source_files:
            return "error" if failed else "stale"
        if current_source is not None and source_files[0].resolve() != current_source.resolve():
            return "error" if failed else "stale"
        source_stat = source_files[0].stat()
        expected_size = metadata.get("source_size")
        expected_mtime = metadata.get("source_mtime_ns")
        if expected_size is not None and int(expected_size) != source_stat.st_size:
            return "error" if failed else "stale"
        if expected_mtime is not None and int(expected_mtime) != source_stat.st_mtime_ns:
            return "error" if failed else "stale"
        return "complete"

    if kind == "summary":
        summary = artifacts / "summary.fa.md"
        failed = (artifacts / ".summary.error.json").is_file()
        if not summary.is_file():
            if not applicable:
                return "not_applicable"
            return "error" if failed else "missing"
        transcript = artifacts / "transcript.fa.txt"
        if not transcript.is_file():
            return "error" if failed else "stale"
        metadata_path = artifacts / "summary.meta.json"
        metadata = _read_json_object(metadata_path)
        current = _summary_metadata(summary, transcript)
        if not metadata:
            _atomic_json(metadata_path, current)
            return "complete"
        if (
            metadata.get("source_sha256") != current["source_sha256"]
            or int(metadata.get("source_size") or -1) != current["source_size"]
            or int(metadata.get("source_mtime_ns") or -1) != current["source_mtime_ns"]
        ):
            return "error" if failed else "stale"
        return "complete"

    questions = artifacts / "questions.fa.md"
    if questions.is_file():
        return "complete"
    return "missing" if applicable else "not_applicable"


def _download_state(activity_directory: Path, item: dict[str, Any]) -> tuple[str, Path | None]:
    filename = str(item.get("filename") or "")
    files_directory = activity_directory / "files"
    final = files_directory / _clean_name(filename, "file") if filename else None
    if final and final.is_file():
        expected_size = item.get("remote_size")
        if expected_size is not None and int(expected_size) != final.stat().st_size:
            return "error", final
        return "complete", final
    partial = final.with_name(final.name + ".part") if final else None
    if partial and partial.is_file():
        return "partial", partial
    if filename or item.get("activity_type") in {"resource", "folder", "page", "book"}:
        return "missing", None
    return "not_applicable", None


def _file_versions(
    public: Path,
    activity_directory: Path,
    current: Path | None,
) -> list[dict[str, Any]]:
    """Describe the current resource and every preserved older version."""
    files_directory = activity_directory / "files"
    if not files_directory.is_dir():
        return []
    candidates = [
        path
        for path in files_directory.rglob("*")
        if path.is_file()
        and not path.name.endswith(".part")
        and not any(part.startswith(".") for part in path.relative_to(files_directory).parts)
    ]
    current_resolved = current.resolve() if current and current.is_file() else None
    records: list[dict[str, Any]] = []
    for path in candidates:
        stat = path.stat()
        is_current = current_resolved is not None and path.resolve() == current_resolved
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        records.append(
            {
                "filename": path.name,
                "path": path.relative_to(activity_directory).as_posix(),
                "url": _relative_browser_path(path, public),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "mimetype": mimetype,
                "state": "current" if is_current else "archived",
            }
        )
    return sorted(
        records,
        key=lambda record: (
            record["state"] != "current",
            -int(record["mtime_ns"]),
            str(record["path"]).casefold(),
        ),
    )


def _reconcile_course(public: Path, course: dict[str, Any]) -> dict[str, Any]:
    course_id = int(course.get("id") or 0)
    downloaded_bytes = 0
    type_counts: dict[str, int] = {}
    state_counts = {
        "downloads": {"complete": 0, "total": 0},
        "transcripts": {"complete": 0, "total": 0},
        "summaries": {"complete": 0, "total": 0},
        "assessments": {"complete": 0, "total": 0},
        "partial": 0,
        "stale": 0,
        "errors": 0,
    }
    records = 0
    for section in course.get("sections", []):
        for item in section.get("items", []):
            records += 1
            activity_directory = _activity_directory(public, course_id, section, item)
            download_state, local_path = _download_state(activity_directory, item)
            media_applicable = item.get("kind") in {"video", "audio"}
            assessment_applicable = item.get("kind") == "assessment" or item.get("activity_type") == "quiz"
            transcript_state = _artifact_state(
                activity_directory,
                "transcript",
                media_applicable,
                current_source=local_path,
            )
            if media_applicable and download_state != "complete" and transcript_state == "complete":
                transcript_state = "stale"
            summary_state = _artifact_state(activity_directory, "summary", media_applicable)
            if (
                media_applicable
                and download_state == "complete"
                and transcript_state != "complete"
                and summary_state == "complete"
            ):
                summary_state = "stale"
            questions_state = _artifact_state(activity_directory, "questions", assessment_applicable)
            for value in (download_state, transcript_state, summary_state, questions_state):
                if value not in STATE_VALUES:
                    raise MoodleError(f"invalid manifest state {value!r}")
            item["state"] = {
                "download": download_state,
                "transcript": transcript_state,
                "summary": summary_state,
                "questions": questions_state,
            }
            item["downloaded"] = download_state == "complete"
            item["transcribed"] = transcript_state == "complete"
            item["summarized"] = summary_state == "complete"
            item["status"] = (
                "downloaded" if download_state == "complete"
                else "online" if download_state == "not_applicable"
                else download_state
            )
            item["artifacts"] = _discover_artifacts(activity_directory, public)
            if item.get("activity_type") == "resource" and item.get("filename"):
                item["file_versions"] = _file_versions(public, activity_directory, local_path)
            else:
                item.pop("file_versions", None)
            if local_path:
                item["local_bytes"] = local_path.stat().st_size
                item["local_url"] = _relative_browser_path(local_path, public)
                if download_state == "complete":
                    item["size"] = local_path.stat().st_size
                    downloaded_bytes += local_path.stat().st_size
            else:
                item["local_bytes"] = 0
                item["local_url"] = None
            if download_state != "not_applicable":
                state_counts["downloads"]["total"] += 1
                state_counts["downloads"]["complete"] += int(download_state == "complete")
            if media_applicable:
                state_counts["transcripts"]["total"] += 1
                state_counts["transcripts"]["complete"] += int(transcript_state == "complete")
                state_counts["summaries"]["total"] += 1
                state_counts["summaries"]["complete"] += int(summary_state == "complete")
            if assessment_applicable:
                state_counts["assessments"]["total"] += 1
                state_counts["assessments"]["complete"] += int(questions_state == "complete")
            state_counts["partial"] += int(download_state == "partial")
            state_counts["stale"] += sum(value == "stale" for value in (transcript_state, summary_state))
            state_counts["errors"] += sum(
                value == "error" for value in (download_state, transcript_state, summary_state, questions_state)
            )
            kind = str(item.get("kind") or "activity")
            type_counts[kind] = type_counts.get(kind, 0) + 1

    course.update(
        {
            "record_count": records,
            "file_count": state_counts["downloads"]["total"],
            "downloaded_count": state_counts["downloads"]["complete"],
            "downloaded_bytes": downloaded_bytes,
            "type_counts": type_counts,
            "states": state_counts,
        }
    )
    return course


def _course_summary(course: dict[str, Any]) -> dict[str, Any]:
    return {
        key: course.get(key)
        for key in (
            "id", "fullname", "shortname", "source_url", "section_count", "activity_count",
            "record_count", "file_count", "downloaded_count", "downloaded_bytes", "type_counts", "states",
        )
    } | {"manifest_url": f"courses/{course.get('id')}/manifest.json"}


def _catalog_from_manifests(courses_root: Path, now: str) -> dict[str, Any]:
    courses: list[dict[str, Any]] = []
    for path in sorted(courses_root.glob("*/manifest.json")):
        course = _read_json_object(path).get("course")
        if isinstance(course, dict):
            courses.append(course)
    courses.sort(key=lambda course: str(course.get("fullname") or "").casefold())
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": now,
        "stats": {
            "courses": len(courses),
            "activities": sum(int(course.get("activity_count") or 0) for course in courses),
            "records": sum(int(course.get("record_count") or 0) for course in courses),
            "files": sum(int(course.get("file_count") or 0) for course in courses),
            "downloaded_files": sum(int(course.get("downloaded_count") or 0) for course in courses),
            "downloaded_bytes": sum(int(course.get("downloaded_bytes") or 0) for course in courses),
        },
        "courses": [_course_summary(course) for course in courses],
    }


def _write_manifests(public: Path, courses: Iterable[dict[str, Any]], *, remote: bool) -> dict[str, Any]:
    courses_root = public / "courses"
    now = _utc_now()
    with _manifest_lock(courses_root):
        for course in courses:
            reconciled = _reconcile_course(public, course)
            manifest_path = courses_root / str(course["id"]) / "manifest.json"
            previous = _read_json_object(manifest_path)
            scan = dict(previous.get("scan") or {})
            scan["local_at"] = now
            if remote:
                scan["remote_at"] = now
            manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "generated_at": now,
                "scan": scan,
                "course": reconciled,
            }
            _atomic_json(manifest_path, manifest)
        catalog = _catalog_from_manifests(courses_root, now)
        _atomic_json(courses_root / "catalog.json", catalog)
    return catalog


def scan_remote(client: MoodleClient, public: Path, course_ids: Iterable[int] = ()) -> dict[str, Any]:
    full_catalog = _build_library_catalog(client, public, course_ids)
    return _write_manifests(public, full_catalog.get("courses", []), remote=True)


def scan_offline(public: Path, course_ids: Iterable[int] = ()) -> dict[str, Any]:
    courses_root = public / "courses"
    selected = set(course_ids)
    manifests = sorted(courses_root.glob("*/manifest.json"))
    courses = []
    for path in manifests:
        manifest = _read_json_object(path)
        course = manifest.get("course")
        if not isinstance(course, dict):
            continue
        course_id = int(course.get("id") or 0)
        if selected and course_id not in selected:
            continue
        courses.append(course)
    missing = selected - {int(course.get("id") or 0) for course in courses}
    if missing:
        raise MoodleError(f"course manifest(s) not found: {', '.join(str(value) for value in sorted(missing))}")
    if not courses:
        raise MoodleError(f"no course manifests found in {courses_root}; run `ravin scan` online first")
    return _write_manifests(public, courses, remote=False)


def update_download_state(public: Path, item: FileItem, path: Path) -> None:
    if not path.is_file():
        return
    manifest_path = public / "courses" / str(item.course_id) / "manifest.json"
    courses_root = public / "courses"
    with _manifest_lock(courses_root):
        manifest = _read_json_object(manifest_path)
        course = manifest.get("course")
        if not isinstance(course, dict):
            return
        now = _utc_now()
        manifest["course"] = _reconcile_course(public, course)
        manifest["generated_at"] = now
        scan = dict(manifest.get("scan") or {})
        scan["local_at"] = now
        manifest["scan"] = scan
        _atomic_json(manifest_path, manifest)
        _atomic_json(courses_root / "catalog.json", _catalog_from_manifests(courses_root, now))


def scan_output(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": course.get("id"),
            "title": course.get("fullname"),
            "sections": course.get("section_count"),
            "activities": course.get("activity_count"),
            "downloads": course.get("states", {}).get("downloads", {}),
            "transcripts": course.get("states", {}).get("transcripts", {}),
            "summaries": course.get("states", {}).get("summaries", {}),
            "assessments": course.get("states", {}).get("assessments", {}),
            "partial": course.get("states", {}).get("partial", 0),
            "stale": course.get("states", {}).get("stale", 0),
            "errors": course.get("states", {}).get("errors", 0),
        }
        for course in catalog.get("courses", [])
    ]


def format_scan(catalog: dict[str, Any]) -> str:
    blocks = []
    for course in scan_output(catalog):
        def count(name: str) -> str:
            state = course[name]
            return f"{int(state.get('complete') or 0):>3} / {int(state.get('total') or 0):<3} complete"

        blocks.append(
            "\n".join(
                (
                    f"Course {course['id']} — {course['title']}",
                    f"{course['activities']} activities across {course['sections']} sections",
                    "",
                    f"Downloads    {count('downloads')}",
                    f"Transcripts  {count('transcripts')}",
                    f"Summaries    {count('summaries')}",
                    f"Assessments  {count('assessments')}",
                    f"Partial      {course['partial']}",
                    f"Stale        {course['stale']}",
                    f"Errors       {course['errors']}",
                )
            )
        )
    return "\n\n".join(blocks)
