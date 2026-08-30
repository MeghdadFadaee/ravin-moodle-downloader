"""Stable activity identity and safe local bundle layout repair."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .models import MoodleError
from .paths import _activity_directory_name, _course_content_root


ACTIVITY_DIRECTORY_PATTERN = re.compile(r"^\d+--\d+--([1-9]\d*)$")


@dataclass(frozen=True)
class LayoutRepairResult:
    course_id: int
    rekeyed: int = 0
    merged: int = 0
    deduplicated: int = 0
    archived_conflicts: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.rekeyed)


@contextmanager
def manifest_lock(courses_root: Path) -> Iterator[None]:
    """Serialize manifest and activity-layout changes for one public root."""
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


def desired_activity_keys(course: Mapping[str, Any]) -> dict[int, str]:
    """Return the current canonical key for every stable activity ID."""
    desired: dict[int, str] = {}
    course_id = int(course.get("id") or 0)
    for section in course.get("sections", []):
        if not isinstance(section, Mapping):
            continue
        for item in section.get("items", []):
            if not isinstance(item, Mapping):
                continue
            try:
                activity_id = int(item.get("activity_id") or 0)
            except (TypeError, ValueError):
                continue
            if activity_id <= 0:
                continue
            key = _activity_directory_name(
                item.get("section_number", section.get("number")),
                item.get("activity_position"),
                activity_id,
                fallback=f"{course_id}\0{item.get('source_url', '')}\0{item.get('title', '')}",
            )
            previous = desired.get(activity_id)
            if previous is not None and previous != key:
                raise MoodleError(
                    f"activity {activity_id} has conflicting positions in course {course_id}: "
                    f"{previous} and {key}"
                )
            desired[activity_id] = key
    return desired


def _activity_id(path: Path) -> int | None:
    match = ACTIVITY_DIRECTORY_PATTERN.fullmatch(path.name)
    return int(match.group(1)) if match else None


def _same_file(left: Path, right: Path) -> bool:
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)
    if not left.is_file() or not right.is_file():
        return False
    left_stat = left.stat()
    right_stat = right.stat()
    if left_stat.st_size != right_stat.st_size:
        return False
    if left_stat.st_ino == right_stat.st_ino and left_stat.st_dev == right_stat.st_dev:
        return True
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(1024 * 1024)
            right_chunk = right_file.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _unique_archive_path(
    canonical: Path,
    relative: Path,
    source_key: str,
    timestamp: str,
) -> Path:
    if relative.parts and relative.parts[0] == "files":
        archive_directory = canonical / "files" / "archive"
    elif relative.parts and relative.parts[0] == "artifacts":
        archive_directory = canonical / "artifacts" / "archive"
    else:
        archive_directory = canonical / "archive"
    archive_directory.mkdir(parents=True, exist_ok=True)
    base = f"{timestamp}--layout-{source_key}--{relative.name}"
    candidate = archive_directory / base
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = archive_directory / f"{timestamp}--{counter}--layout-{source_key}--{relative.name}"
        counter += 1
    return candidate


def _merge_entry(
    source: Path,
    target: Path,
    *,
    source_root: Path,
    canonical: Path,
    timestamp: str,
) -> tuple[int, int]:
    """Merge one source entry, returning deduplicated and archived counts."""
    if not target.exists() and not target.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return 0, 0

    source_is_directory = source.is_dir() and not source.is_symlink()
    target_is_directory = target.is_dir() and not target.is_symlink()
    if source_is_directory and target_is_directory:
        deduplicated = 0
        archived = 0
        for child in sorted(source.iterdir(), key=lambda path: path.name.casefold()):
            child_deduplicated, child_archived = _merge_entry(
                child,
                target / child.name,
                source_root=source_root,
                canonical=canonical,
                timestamp=timestamp,
            )
            deduplicated += child_deduplicated
            archived += child_archived
        source.rmdir()
        return deduplicated, archived

    if not source_is_directory and not target_is_directory and _same_file(source, target):
        source.unlink()
        return 1, 0

    relative = source.relative_to(source_root)
    archived_path = _unique_archive_path(canonical, relative, source_root.name, timestamp)
    os.replace(source, archived_path)
    return 0, 1


def _merge_bundle(source: Path, canonical: Path, timestamp: str) -> tuple[int, int]:
    deduplicated = 0
    archived = 0
    for entry in sorted(source.iterdir(), key=lambda path: path.name.casefold()):
        entry_deduplicated, entry_archived = _merge_entry(
            entry,
            canonical / entry.name,
            source_root=source,
            canonical=canonical,
            timestamp=timestamp,
        )
        deduplicated += entry_deduplicated
        archived += entry_archived
    source.rmdir()
    return deduplicated, archived


def repair_activity_layout(
    public: Path,
    course_id: int,
    desired: Mapping[int, str],
    *,
    assume_locked: bool = False,
) -> LayoutRepairResult:
    """Move existing bundles to the current sortable key using activity ID identity."""
    public = public.expanduser().resolve()
    content_root = _course_content_root(public, course_id)
    if not content_root.is_dir() or not desired:
        return LayoutRepairResult(course_id=course_id)

    for activity_id, canonical_key in desired.items():
        match = ACTIVITY_DIRECTORY_PATTERN.fullmatch(canonical_key)
        if activity_id <= 0 or match is None or int(match.group(1)) != activity_id:
            raise MoodleError(
                f"invalid canonical activity key for course {course_id}, activity {activity_id}: "
                f"{canonical_key!r}"
            )

    def perform() -> LayoutRepairResult:
        by_activity: dict[int, list[Path]] = {}
        for path in content_root.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            activity_id = _activity_id(path)
            if activity_id is not None:
                by_activity.setdefault(activity_id, []).append(path)

        rekeyed = 0
        merged = 0
        deduplicated = 0
        archived_conflicts = 0
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for activity_id, canonical_key in sorted(desired.items()):
            if activity_id <= 0:
                continue
            canonical = content_root / canonical_key
            sources = sorted(
                (path for path in by_activity.get(activity_id, []) if path.name != canonical_key),
                key=lambda path: path.name.casefold(),
            )
            for source in sources:
                try:
                    if not canonical.exists() and not canonical.is_symlink():
                        os.replace(source, canonical)
                    else:
                        if not canonical.is_dir() or canonical.is_symlink():
                            raise MoodleError(f"canonical activity path is not a directory: {canonical}")
                        duplicate_count, archive_count = _merge_bundle(source, canonical, timestamp)
                        merged += 1
                        deduplicated += duplicate_count
                        archived_conflicts += archive_count
                    rekeyed += 1
                except MoodleError:
                    raise
                except OSError as exc:
                    raise MoodleError(
                        f"could not rekey course {course_id} activity {activity_id} "
                        f"from {source.name} to {canonical_key}: {exc}"
                    ) from exc
        return LayoutRepairResult(
            course_id=course_id,
            rekeyed=rekeyed,
            merged=merged,
            deduplicated=deduplicated,
            archived_conflicts=archived_conflicts,
        )

    if assume_locked:
        return perform()
    with manifest_lock(public / "courses"):
        return perform()


def repair_courses_layout(
    public: Path,
    courses: Iterable[dict[str, Any]],
    *,
    assume_locked: bool = False,
) -> list[LayoutRepairResult]:
    results: list[LayoutRepairResult] = []
    for course in courses:
        course_id = int(course.get("id") or 0)
        if course_id <= 0:
            continue
        result = repair_activity_layout(
            public,
            course_id,
            desired_activity_keys(course),
            assume_locked=assume_locked,
        )
        if result.changed:
            results.append(result)
    return results


def format_layout_repair(result: LayoutRepairResult) -> str:
    detail: list[str] = []
    if result.merged:
        detail.append(f"merged {result.merged} {'collision' if result.merged == 1 else 'collisions'}")
    if result.deduplicated:
        file_label = "file" if result.deduplicated == 1 else "files"
        detail.append(f"deduplicated {result.deduplicated} identical {file_label}")
    if result.archived_conflicts:
        conflict_label = "conflict" if result.archived_conflicts == 1 else "conflicts"
        detail.append(f"archived {result.archived_conflicts} {conflict_label}")
    suffix = f" ({', '.join(detail)})" if detail else ""
    bundle_label = "bundle" if result.rekeyed == 1 else "bundles"
    return f"Rekeyed {result.rekeyed} activity {bundle_label} for course {result.course_id}{suffix}."
