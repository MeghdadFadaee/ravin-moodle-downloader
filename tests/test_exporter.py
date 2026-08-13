from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from re import fullmatch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.exporter import export_courses, format_export_result
from ravin.models import MoodleError


class CourseExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.public = self.root / "public"
        courses = self.public / "courses"
        (courses / "44" / "content" / "001--001--100" / "files" / "archive").mkdir(parents=True)
        (courses / "51").mkdir(parents=True)
        (courses / "catalog.json").write_text('{"courses": []}\n', encoding="utf-8")
        for course_id in (44, 51):
            (courses / str(course_id) / "manifest.json").write_text(
                json.dumps({"course": {"id": course_id}}),
                encoding="utf-8",
            )
        bundle = courses / "44" / "content" / "001--001--100"
        (bundle / "files" / "notes.pdf").write_bytes(b"pdf notes")
        (bundle / "files" / "lesson.mp4").write_bytes(b"current video")
        (bundle / "files" / "archive" / "old.MKV").write_bytes(b"archived video")
        (bundle / "files" / "audio.ogg").write_bytes(b"audio remains included")
        (bundle / "files" / "unfinished.mp4.part").write_bytes(b"partial")
        (bundle / "artifacts").mkdir()
        (bundle / "artifacts" / "transcript.fa.txt").write_text("transcript", encoding="utf-8")
        (courses / ".manifest.lock").write_text("", encoding="utf-8")
        (courses / ".DS_Store").write_bytes(b"metadata")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def archive_names(self, path: Path) -> set[str]:
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            return set(archive.namelist())

    def test_default_export_excludes_videos_but_keeps_course_data(self) -> None:
        result = export_courses(self.public)
        output = Path(result.output)
        names = self.archive_names(output)

        self.assertEqual(output.parent, (self.public / "exports").resolve())
        self.assertIsNotNone(fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.zip", output.name))
        self.assertIn("courses/catalog.json", names)
        self.assertIn("courses/44/manifest.json", names)
        self.assertIn("courses/51/manifest.json", names)
        self.assertIn("courses/44/content/001--001--100/files/notes.pdf", names)
        self.assertIn("courses/44/content/001--001--100/files/audio.ogg", names)
        self.assertIn("courses/44/content/001--001--100/artifacts/transcript.fa.txt", names)
        self.assertNotIn("courses/44/content/001--001--100/files/lesson.mp4", names)
        self.assertNotIn("courses/44/content/001--001--100/files/archive/old.MKV", names)
        self.assertFalse(any(name.endswith(".part") for name in names))
        self.assertFalse(any(name.endswith(".DS_Store") or name.endswith(".manifest.lock") for name in names))
        self.assertEqual(result.courses, 2)
        self.assertEqual(result.videos_skipped, 2)
        self.assertEqual(result.videos_included, 0)
        self.assertIn("skipped 2 video(s)", format_export_result(result))

    def test_include_videos_exports_current_and_archived_videos(self) -> None:
        output = self.root / "complete-courses.zip"

        result = export_courses(self.public, output, include_videos=True)
        names = self.archive_names(output)

        self.assertIn("courses/44/content/001--001--100/files/lesson.mp4", names)
        self.assertIn("courses/44/content/001--001--100/files/archive/old.MKV", names)
        self.assertEqual(result.videos_included, 2)
        self.assertEqual(result.videos_skipped, 0)

    def test_export_atomically_replaces_an_existing_zip(self) -> None:
        output = self.root / "courses.zip"
        output.write_bytes(b"not a zip")

        export_courses(self.public, output)

        self.assertIn("courses/catalog.json", self.archive_names(output))
        self.assertEqual(list(self.root.glob(".courses.zip.*.tmp")), [])

    def test_rejects_output_inside_public_courses(self) -> None:
        with self.assertRaisesRegex(MoodleError, "outside public/courses"):
            export_courses(self.public, self.public / "courses" / "export.zip")

    def test_requires_scanned_courses_and_zip_extension(self) -> None:
        empty_public = self.root / "empty"
        with self.assertRaisesRegex(MoodleError, "run `ravin scan` first"):
            export_courses(empty_public, self.root / "empty.zip")
        with self.assertRaisesRegex(MoodleError, r"\.zip extension"):
            export_courses(self.public, self.root / "courses.backup")


if __name__ == "__main__":
    unittest.main()
