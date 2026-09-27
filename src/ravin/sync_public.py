"""Synchronize the public learning library to an SSH destination."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .models import MoodleError


VIDEO_PATTERNS = (
    "*.[mM][pP]4", "*.[mM][pP]4.part",
    "*.[mM]4[vV]", "*.[mM]4[vV].part",
    "*.[mM][kK][vV]", "*.[mM][kK][vV].part",
    "*.[mM][oO][vV]", "*.[mM][oO][vV].part",
    "*.[aA][vV][iI]", "*.[aA][vV][iI].part",
    "*.[wW][eE][bB][mM]", "*.[wW][eE][bB][mM].part",
    "*.[fF][lL][vV]", "*.[fF][lL][vV].part",
    "*.[wW][mM][vV]", "*.[wW][mM][vV].part",
    "*.[mM][pP][eE][gG]", "*.[mM][pP][eE][gG].part",
    "*.[mM][pP][gG]", "*.[mM][pP][gG].part",
    "*.[3][gG][pP]", "*.[3][gG][pP].part",
    "*.[oO][gG][vV]", "*.[oO][gG][vV].part",
)


def sync_public(
    public: Path,
    destination: str | None,
    *,
    dry_run: bool = False,
    delete: bool = False,
) -> int:
    """Run rsync for non-video public files and return its exit code."""
    resolved_destination = destination or os.getenv("SYNC_PUBLIC_DESTINATION")
    if not resolved_destination:
        raise MoodleError(
            "no sync destination provided; use --destination USER@HOST:PATH "
            "or set SYNC_PUBLIC_DESTINATION"
        )
    source = public.expanduser().resolve()
    if not source.is_dir():
        raise MoodleError(f"public web root does not exist: {source}")
    rsync = shutil.which("rsync")
    if rsync is None:
        raise MoodleError("rsync is not installed locally")

    command = [
        rsync,
        "--archive",
        "--compress",
        "--human-readable",
        "--partial",
        "--progress",
        "--itemize-changes",
    ]
    command.extend(f"--exclude={pattern}" for pattern in VIDEO_PATTERNS)
    if dry_run:
        command.append("--dry-run")
    if delete:
        command.append("--delete")
    command.extend(("--", f"{source}/", resolved_destination))

    print(f"Syncing {source}/ to {resolved_destination}")
    if dry_run:
        print("Dry run: no files will be changed.")
    if delete:
        print("Mirror mode: stale remote non-video files will be deleted.")
    return subprocess.run(command, check=False).returncode
