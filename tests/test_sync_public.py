from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.models import MoodleError
from ravin.sync_public import VIDEO_PATTERNS, sync_public


class SyncPublicTests(unittest.TestCase):
    def test_builds_video_excluding_rsync_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "public"
            public.mkdir()
            completed = subprocess.CompletedProcess([], 0)
            with (
                patch("ravin.sync_public.shutil.which", return_value="/usr/bin/rsync"),
                patch("ravin.sync_public.subprocess.run", return_value=completed) as run,
            ):
                exit_code = sync_public(
                    public,
                    "user@example:/srv/public/",
                    dry_run=True,
                    delete=True,
                )

        self.assertEqual(exit_code, 0)
        command = run.call_args.args[0]
        self.assertIn("--dry-run", command)
        self.assertIn("--delete", command)
        self.assertEqual(
            [argument for argument in command if argument.startswith("--exclude=")],
            [f"--exclude={pattern}" for pattern in VIDEO_PATTERNS],
        )
        self.assertEqual(command[-3:], ["--", f"{public.resolve()}/", "user@example:/srv/public/"])

    def test_requires_a_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(MoodleError, "no sync destination"):
                sync_public(Path(directory), None)


if __name__ == "__main__":
    unittest.main()
