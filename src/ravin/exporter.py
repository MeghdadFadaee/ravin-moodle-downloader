"""Portable ZIP exports of local course data."""

from __future__ import annotations

import mimetypes
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import MoodleError


VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".divx", ".f4v", ".flv", ".m2ts", ".m4v",
    ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogv", ".rm",
    ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
TRANSIENT_NAMES = {".DS_Store", ".manifest.lock"}


@dataclass(frozen=True)
class ExportResult:
    output: str
    courses: int
    files: int
    source_bytes: int
    archive_bytes: int
    videos_included: int
    videos_skipped: int
    skipped_video_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_video(path: Path) -> bool:
    mimetype = mimetypes.guess_type(path.name)[0] or ""
    return mimetype.startswith("video/") or path.suffix.casefold() in VIDEO_EXTENSIONS


def _exportable_files(courses_root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in courses_root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.name not in TRANSIENT_NAMES
            and not path.name.endswith(".part")
        ),
        key=lambda path: path.relative_to(courses_root).as_posix().casefold(),
    )


def export_courses(
    public: Path,
    output: Path | None = None,
    *,
    include_videos: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ExportResult:
    """Create an atomic ZIP containing all local course data."""
    public = public.expanduser().resolve()
    courses_root = (public / "courses").resolve()
    if not courses_root.is_dir() or not any(courses_root.glob("*/manifest.json")):
        raise MoodleError(f"no local courses found in {courses_root}; run `ravin scan` first")

    if output is None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        output = public / "exports" / f"{timestamp}.zip"
    output = output.expanduser().resolve()
    if output.suffix.casefold() != ".zip":
        raise MoodleError("export output must use a .zip extension")
    try:
        output.relative_to(courses_root)
    except ValueError:
        pass
    else:
        raise MoodleError("export output must be outside public/courses so it is not exposed by the library server")
    if output.exists() and not output.is_file():
        raise MoodleError(f"export output is not a file: {output}")

    candidates = _exportable_files(courses_root)
    videos = [path for path in candidates if _is_video(path)]
    included = candidates if include_videos else [path for path in candidates if not _is_video(path)]
    source_bytes = sum(path.stat().st_size for path in included)
    skipped_video_bytes = 0 if include_videos else sum(path.stat().st_size for path in videos)
    course_count = sum(1 for _path in courses_root.glob("*/manifest.json"))
    if progress:
        video_note = "including videos" if include_videos else f"excluding {len(videos)} video(s)"
        progress(f"Creating {output.name} from {len(included)} file(s), {video_note}...")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
            strict_timestamps=False,
        ) as archive:
            for index, path in enumerate(included, start=1):
                archive_name = (Path("courses") / path.relative_to(courses_root)).as_posix()
                compression = zipfile.ZIP_STORED if _is_video(path) else zipfile.ZIP_DEFLATED
                archive.write(path, archive_name, compress_type=compression)
                if progress and (index % 100 == 0 or index == len(included)):
                    progress(f"  added {index}/{len(included)} file(s)")
        os.replace(temporary, output)
        temporary = None
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise MoodleError(f"could not create course export: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return ExportResult(
        output=str(output),
        courses=course_count,
        files=len(included),
        source_bytes=source_bytes,
        archive_bytes=output.stat().st_size,
        videos_included=len(videos) if include_videos else 0,
        videos_skipped=0 if include_videos else len(videos),
        skipped_video_bytes=skipped_video_bytes,
    )


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_export_result(result: ExportResult) -> str:
    video_detail = (
        f"included {result.videos_included} video(s)"
        if result.videos_included
        else f"skipped {result.videos_skipped} video(s) ({_format_size(result.skipped_video_bytes)})"
    )
    return (
        f"Created {result.output}\n"
        f"{result.courses} course(s), {result.files} file(s), {video_detail}.\n"
        f"Archive size: {_format_size(result.archive_bytes)} "
        f"from {_format_size(result.source_bytes)} of included data."
    )
