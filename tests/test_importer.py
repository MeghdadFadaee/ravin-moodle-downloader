from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.importer import format_import_result, import_courses
from ravin.models import MoodleError


class _Response:
    def __init__(self, body: bytes, *, declared_size: int | None = None):
        self._body = io.BytesIO(body)
        size = len(body) if declared_size is None else declared_size
        self.headers = {"Content-Length": str(size)}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return 200

    def read(self, size: int):
        return self._body.read(size)


def _manifest(course_id: int, title: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "course": {
                "id": course_id,
                "fullname": title,
                "shortname": "",
                "sections": [],
                "section_count": 0,
                "activity_count": 0,
            },
        }
    ).encode()


def _archive(files: dict[str, bytes], *, explicit_directories: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if explicit_directories:
            archive.writestr("courses/", b"")
            archive.writestr("courses/44/", b"")
        for name, value in files.items():
            archive.writestr(name, value)
    return output.getvalue()


class CourseImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.public = self.root / "public"
        course = self.public / "courses" / "44"
        course.mkdir(parents=True)
        (course / "manifest.json").write_bytes(_manifest(44, "Old title"))
        (course / "conflict.txt").write_text("local version", encoding="utf-8")
        (course / "local-only.txt").write_text("keep me", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_import_merges_archive_priority_and_runs_offline_scan(self) -> None:
        body = _archive(
            {
                "courses/catalog.json": b"{}",
                "courses/44/manifest.json": _manifest(44, "Imported title"),
                "courses/44/conflict.txt": b"archive version",
                "courses/44/imported.txt": b"new file",
                "courses/51/manifest.json": _manifest(51, "Second course"),
            },
            explicit_directories=True,
        )
        url = "https://mirror.example/exports/backup.zip?signature=secret"

        with patch("ravin.importer.urllib.request.urlopen", return_value=_Response(body)) as urlopen:
            result, catalog = import_courses(self.public, url)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(result.source, "https://mirror.example/exports/backup.zip")
        self.assertEqual(result.courses, 2)
        self.assertEqual(result.files, 5)
        self.assertEqual(result.added, 3)
        self.assertEqual(result.replaced, 2)
        self.assertEqual((self.public / "courses/44/conflict.txt").read_text(), "archive version")
        self.assertEqual((self.public / "courses/44/local-only.txt").read_text(), "keep me")
        self.assertEqual((self.public / "courses/44/imported.txt").read_text(), "new file")
        self.assertEqual(catalog["stats"]["courses"], 2)
        refreshed = json.loads((self.public / "courses/44/manifest.json").read_text())
        self.assertEqual(refreshed["course"]["fullname"], "Imported title")
        self.assertIn("3 added, 2 replaced", format_import_result(result))

    def test_unsafe_archive_is_rejected_before_existing_files_change(self) -> None:
        body = _archive(
            {
                "courses/44/manifest.json": _manifest(44, "Imported title"),
                "courses/../../outside.txt": b"unsafe",
            }
        )

        with patch("ravin.importer.urllib.request.urlopen", return_value=_Response(body)):
            with self.assertRaisesRegex(MoodleError, "unsafe path"):
                import_courses(self.public, "https://mirror.example/unsafe.zip")

        self.assertEqual((self.public / "courses/44/conflict.txt").read_text(), "local version")
        current = json.loads((self.public / "courses/44/manifest.json").read_text())
        self.assertEqual(current["course"]["fullname"], "Old title")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_rejects_invalid_url_zip_manifest_and_incomplete_download(self) -> None:
        with self.assertRaisesRegex(MoodleError, "direct http"):
            import_courses(self.public, "file:///tmp/export.zip")

        with patch("ravin.importer.urllib.request.urlopen", return_value=_Response(b"not a zip")):
            with self.assertRaisesRegex(MoodleError, "not a valid ZIP"):
                import_courses(self.public, "https://mirror.example/not-a-zip")

        mismatched = _archive({"courses/44/manifest.json": _manifest(51, "Wrong")})
        with patch("ravin.importer.urllib.request.urlopen", return_value=_Response(mismatched)):
            with self.assertRaisesRegex(MoodleError, "does not match"):
                import_courses(self.public, "https://mirror.example/mismatch.zip")

        valid = _archive({"courses/44/manifest.json": _manifest(44, "Course")})
        with patch(
            "ravin.importer.urllib.request.urlopen",
            return_value=_Response(valid, declared_size=len(valid) + 10),
        ):
            with self.assertRaisesRegex(MoodleError, "incomplete"):
                import_courses(self.public, "https://mirror.example/incomplete.zip")


if __name__ == "__main__":
    unittest.main()
