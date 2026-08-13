"""Restore or mirror course exports from a direct ZIP URL."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .constants import USER_AGENT
from .models import MoodleError
from .scan import _manifest_lock, scan_offline


@dataclass(frozen=True)
class ImportResult:
    source: str
    archive_bytes: int
    courses: int
    files: int
    added: int
    replaced: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _display_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parsed.port
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise MoodleError("import source must be a direct http:// or https:// URL")
    try:
        parsed.port
    except ValueError as exc:
        raise MoodleError(f"invalid import URL: {exc}") from exc


def _download(
    url: str,
    destination: Path,
    *,
    timeout: int,
    progress: Callable[[str], None] | None,
) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    display_url = _display_url(url)
    if progress:
        progress(f"Downloading {display_url}...")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            if status is not None and not 200 <= status < 300:
                raise MoodleError(f"export download returned HTTP {status}")
            expected_value = response.headers.get("Content-Length")
            expected = int(expected_value) if expected_value and expected_value.isdigit() else None
            downloaded = 0
            next_update = 64 * 1024 * 1024
            with destination.open("wb") as target:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    downloaded += len(chunk)
                    if progress and downloaded >= next_update:
                        suffix = f" / {_format_size(expected)}" if expected is not None else ""
                        progress(f"  downloaded {_format_size(downloaded)}{suffix}")
                        next_update += 64 * 1024 * 1024
    except MoodleError:
        raise
    except urllib.error.HTTPError as exc:
        raise MoodleError(f"could not download export: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MoodleError(f"could not download export: {exc.reason}") from exc
    except http.client.HTTPException as exc:
        raise MoodleError(f"could not download export: {exc}") from exc
    except OSError as exc:
        raise MoodleError(f"could not save downloaded export: {exc}") from exc
    if downloaded == 0:
        raise MoodleError("downloaded export is empty")
    if expected is not None and downloaded != expected:
        raise MoodleError(f"export download was incomplete: expected {expected} bytes, received {downloaded}")
    return downloaded


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    if not name or "\\" in name or "\0" in name:
        raise MoodleError(f"unsafe path in export archive: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MoodleError(f"unsafe path in export archive: {name!r}")
    root_directory = info.is_dir() and path.parts == ("courses",)
    if not path.parts or path.parts[0] != "courses" or (len(path.parts) < 2 and not root_directory):
        raise MoodleError(f"unexpected path in export archive: {name!r}; expected courses/...")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise MoodleError(f"symbolic links are not allowed in export archives: {name!r}")
    if info.flag_bits & 0x1:
        raise MoodleError(f"encrypted files are not supported in export archives: {name!r}")
    return path


def _validated_members(archive: zipfile.ZipFile) -> tuple[list[tuple[zipfile.ZipInfo, PurePosixPath]], set[int]]:
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    seen: set[str] = set()
    file_paths: set[tuple[str, ...]] = set()
    for info in archive.infolist():
        path = _member_path(info)
        key = path.as_posix().casefold().rstrip("/")
        if key in seen:
            raise MoodleError(f"duplicate path in export archive: {path.as_posix()!r}")
        seen.add(key)
        if not info.is_dir():
            file_paths.add(tuple(part.casefold() for part in path.parts))
            members.append((info, path))

    for parts in file_paths:
        if any(parts[:index] in file_paths for index in range(2, len(parts))):
            raise MoodleError(f"file/directory conflict in export archive: {'/'.join(parts)!r}")

    course_ids: set[int] = set()
    for info, path in members:
        if len(path.parts) != 3 or path.parts[2] != "manifest.json":
            continue
        try:
            course_id = int(path.parts[1])
            manifest = json.loads(archive.read(info))
            manifest_course_id = int(manifest["course"]["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MoodleError(f"invalid course manifest in export: {path.as_posix()}") from exc
        if course_id <= 0 or manifest_course_id != course_id:
            raise MoodleError(f"course ID does not match manifest path: {path.as_posix()}")
        course_ids.add(course_id)
    if not course_ids:
        raise MoodleError("archive contains no valid courses; expected courses/COURSE_ID/manifest.json")
    return members, course_ids


def _extract_to_staging(
    archive: zipfile.ZipFile,
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]],
    staging: Path,
) -> None:
    try:
        for info, path in members:
            target = staging.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise MoodleError(f"could not validate export contents: {exc}") from exc


def _merge_staging(staging_courses: Path, courses_root: Path) -> tuple[int, int]:
    sources = sorted(path for path in staging_courses.rglob("*") if path.is_file())
    if courses_root.is_symlink():
        raise MoodleError(f"course directory cannot be a symbolic link during import: {courses_root}")
    courses_root.mkdir(parents=True, exist_ok=True)
    resolved_root = courses_root.resolve()
    plans: list[tuple[Path, Path, bool]] = []
    for source in sources:
        relative = source.relative_to(staging_courses)
        target = courses_root / relative
        try:
            target.resolve(strict=False).relative_to(resolved_root)
        except ValueError as exc:
            raise MoodleError(f"unsafe existing path blocks import: {target}") from exc
        if target.exists() and not target.is_file():
            raise MoodleError(f"cannot replace non-file path during import: {target}")
        parent = target.parent
        while parent != courses_root:
            if parent.exists() and not parent.is_dir():
                raise MoodleError(f"cannot create import directory over a file: {parent}")
            parent = parent.parent
        plans.append((source, target, target.is_file()))

    added = 0
    replaced = 0
    with _manifest_lock(courses_root):
        for source, target, existed in plans:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.ravin-import-",
                dir=target.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            replaced += int(existed)
            added += int(not existed)
    return added, replaced


def import_courses(
    public: Path,
    url: str,
    *,
    timeout: int = 60,
    progress: Callable[[str], None] | None = None,
) -> tuple[ImportResult, dict[str, Any]]:
    """Download, validate, merge, and offline-scan a course export."""
    _validate_url(url)
    if timeout <= 0:
        raise MoodleError("import timeout must be greater than zero")
    public = public.expanduser().resolve()
    courses_root = public / "courses"
    with tempfile.TemporaryDirectory(prefix="ravin-import-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        archive_path = temporary_root / "download.zip"
        staging = temporary_root / "staging"
        archive_bytes = _download(url, archive_path, timeout=timeout, progress=progress)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members, course_ids = _validated_members(archive)
                if progress:
                    progress(f"Validating {len(members)} file(s) from {len(course_ids)} course(s)...")
                _extract_to_staging(archive, members, staging)
        except zipfile.BadZipFile as exc:
            raise MoodleError(f"downloaded file is not a valid ZIP archive: {exc}") from exc

        if progress:
            progress("Merging imported files; archive versions take priority...")
        try:
            added, replaced = _merge_staging(staging / "courses", courses_root)
        except MoodleError:
            raise
        except OSError as exc:
            raise MoodleError(f"could not merge imported course files: {exc}") from exc

    if progress:
        progress("Running offline scan...")
    catalog = scan_offline(public)
    return (
        ImportResult(
            source=_display_url(url),
            archive_bytes=archive_bytes,
            courses=len(course_ids),
            files=len(members),
            added=added,
            replaced=replaced,
        ),
        catalog,
    )


def format_import_result(result: ImportResult) -> str:
    return (
        f"Imported {result.courses} course(s) from {result.source}\n"
        f"{result.files} file(s): {result.added} added, {result.replaced} replaced "
        f"({_format_size(result.archive_bytes)} downloaded)."
    )
