"""Safe local file installation and version preservation."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy a local file into place without exposing a partial destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        os.chmod(destination, 0o644)
        return
    temporary_path: Path | None = None
    try:
        with source.open("rb") as input_file, tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
            temporary_path = Path(output_file.name)
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def archive_existing_file(path: Path) -> Path:
    """Preserve a file under its activity's archive directory."""
    archive_directory = path.parent / "archive"
    archive_directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    candidate = archive_directory / f"{timestamp}--{path.name}"
    counter = 2
    while candidate.exists():
        candidate = archive_directory / f"{timestamp}--{counter}--{path.name}"
        counter += 1
    try:
        os.link(path, candidate)
    except OSError:
        shutil.copy2(path, candidate)
    return candidate
