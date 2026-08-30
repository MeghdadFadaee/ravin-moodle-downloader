from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin import Course, FileItem
from ravin.layout import format_layout_repair, repair_activity_layout
from ravin.models import MoodleError
from ravin.scan import scan_offline, scan_remote


class ActivityLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.public = self.root / "public"
        self.content = self.public / "courses" / "51" / "content"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_rekeys_complete_bundle_by_stable_activity_id(self) -> None:
        old = self.content / "002--001--5222"
        media = old / "files" / "Intro01.mp4"
        transcript = old / "artifacts" / "transcript.fa.txt"
        media.parent.mkdir(parents=True)
        transcript.parent.mkdir(parents=True)
        media.write_bytes(b"video")
        transcript.write_text("transcript", encoding="utf-8")
        (old / "artifacts/questions.fa.md").write_text("questions", encoding="utf-8")
        (old / "artifacts/recording.meta.json").write_text(
            '{"source":"../files/Intro01.mp4"}',
            encoding="utf-8",
        )
        (old / "files/archive").mkdir()
        (old / "files/archive/previous.mp4").write_bytes(b"previous")
        original_mtime = media.stat().st_mtime_ns
        orphan = self.content / "003--001--9999"
        orphan.mkdir(parents=True)
        idless = self.content / "custom-bundle"
        idless.mkdir()

        result = repair_activity_layout(self.public, 51, {5222: "004--001--5222"})

        canonical = self.content / "004--001--5222"
        self.assertFalse(old.exists())
        self.assertEqual((canonical / "files/Intro01.mp4").read_bytes(), b"video")
        self.assertEqual((canonical / "artifacts/transcript.fa.txt").read_text(), "transcript")
        self.assertEqual((canonical / "artifacts/questions.fa.md").read_text(), "questions")
        self.assertTrue((canonical / "artifacts/recording.meta.json").is_file())
        self.assertEqual((canonical / "files/archive/previous.mp4").read_bytes(), b"previous")
        self.assertEqual((canonical / "files/Intro01.mp4").stat().st_mtime_ns, original_mtime)
        self.assertTrue(orphan.is_dir())
        self.assertTrue(idless.is_dir())
        self.assertEqual(result.rekeyed, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(
            format_layout_repair(result),
            "Rekeyed 1 activity bundle for course 51.",
        )

    def test_merges_collisions_with_canonical_priority_and_no_data_loss(self) -> None:
        canonical = self.content / "004--001--5222"
        old = self.content / "002--001--5222"
        older = self.content / "003--009--5222"
        for directory in (canonical, old, older):
            (directory / "files").mkdir(parents=True)
        (canonical / "files/current.mp4").write_bytes(b"current LMS version")
        (canonical / "files/same.pdf").write_bytes(b"identical")
        (canonical / "artifacts").mkdir()
        (canonical / "artifacts/transcript.fa.txt").write_text("canonical transcript", encoding="utf-8")

        (old / "files/current.mp4").write_bytes(b"older version")
        (old / "files/same.pdf").write_bytes(b"identical")
        (old / "files/notes.pdf").write_bytes(b"notes")
        (old / "files/resume.mp4.part").write_bytes(b"partial download")
        (old / "files/archive").mkdir()
        (old / "files/archive/previous.mp4").write_bytes(b"previous version")
        (old / "artifacts").mkdir()
        (old / "artifacts/transcript.fa.txt").write_text("old transcript", encoding="utf-8")
        (old / "artifacts/summary.fa.md").write_text("summary", encoding="utf-8")
        (older / "files/extra.txt").write_text("extra", encoding="utf-8")

        result = repair_activity_layout(self.public, 51, {5222: canonical.name})

        self.assertFalse(old.exists())
        self.assertFalse(older.exists())
        self.assertEqual((canonical / "files/current.mp4").read_bytes(), b"current LMS version")
        self.assertEqual((canonical / "files/notes.pdf").read_bytes(), b"notes")
        self.assertEqual((canonical / "files/extra.txt").read_text(), "extra")
        self.assertEqual((canonical / "files/resume.mp4.part").read_bytes(), b"partial download")
        self.assertEqual((canonical / "files/archive/previous.mp4").read_bytes(), b"previous version")
        self.assertEqual((canonical / "artifacts/transcript.fa.txt").read_text(), "canonical transcript")
        self.assertEqual((canonical / "artifacts/summary.fa.md").read_text(), "summary")
        archived_media = list((canonical / "files/archive").glob("*--current.mp4"))
        archived_artifact = list((canonical / "artifacts/archive").glob("*--transcript.fa.txt"))
        self.assertEqual([path.read_bytes() for path in archived_media], [b"older version"])
        self.assertEqual([path.read_text() for path in archived_artifact], ["old transcript"])
        self.assertEqual(result.rekeyed, 2)
        self.assertEqual(result.merged, 2)
        self.assertEqual(result.deduplicated, 1)
        self.assertEqual(result.archived_conflicts, 2)

    def test_rejects_unsafe_or_mismatched_canonical_key(self) -> None:
        (self.content / "002--001--5222").mkdir(parents=True)
        for key in ("../../outside", "004--001--9999"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(MoodleError, "invalid canonical activity key"):
                    repair_activity_layout(self.public, 51, {5222: key})

    def test_offline_scan_repairs_manifest_layout_and_restores_artifact_states(self) -> None:
        old = self.content / "002--001--5222"
        media = old / "files/Intro01.mp4"
        artifacts = old / "artifacts"
        media.parent.mkdir(parents=True)
        artifacts.mkdir(parents=True)
        media.write_bytes(b"video")
        transcript = artifacts / "transcript.fa.txt"
        transcript.write_text("transcript", encoding="utf-8")
        media_stat = media.stat()
        (artifacts / "transcript.meta.json").write_text(
            json.dumps(
                {
                    "source": "../files/Intro01.mp4",
                    "source_size": media_stat.st_size,
                    "source_mtime_ns": media_stat.st_mtime_ns,
                }
            ),
            encoding="utf-8",
        )
        (artifacts / "summary.fa.md").write_text("summary", encoding="utf-8")
        manifest = self.public / "courses/51/manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "course": {
                        "id": 51,
                        "fullname": "LPIC",
                        "sections": [
                            {
                                "number": 4,
                                "items": [
                                    {
                                        "filename": "Intro01.mp4",
                                        "activity_type": "resource",
                                        "kind": "video",
                                        "title": "Intro",
                                        "section_number": 4,
                                        "activity_position": 1,
                                        "activity_id": 5222,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

        catalog = scan_offline(self.public, [51])

        canonical = self.content / "004--001--5222"
        refreshed = json.loads(manifest.read_text(encoding="utf-8"))
        item = refreshed["course"]["sections"][0]["items"][0]
        self.assertTrue(canonical.is_dir())
        self.assertFalse(old.exists())
        self.assertEqual(item["key"], "004--001--5222")
        self.assertEqual(item["state"]["download"], "complete")
        self.assertEqual(item["state"]["transcript"], "complete")
        self.assertEqual(item["state"]["summary"], "complete")
        self.assertEqual(catalog["stats"]["downloaded_files"], 1)

    def test_online_scan_repairs_before_catalog_discovers_local_files(self) -> None:
        old = self.content / "002--001--5222"
        media = old / "files/Intro01.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"video")

        class FakeClient:
            site = "https://training.example"

            def list_courses(self):
                return [Course(51, "LPIC")]

            def list_files(self, _course_id):
                return [
                    FileItem(
                        51,
                        "Introduction",
                        "Intro",
                        "Intro01.mp4",
                        "https://training.example/pluginfile.php/intro.mp4",
                        "video/mp4",
                        5,
                        section_number=4,
                        activity_id=5222,
                        activity_type="resource",
                        activity_position=1,
                    )
                ]

            def course_structure(self, _course_id):
                return [
                    {
                        "id": 200,
                        "number": 4,
                        "name": "Introduction",
                        "activities": [
                            {
                                "id": 5222,
                                "position": 1,
                                "name": "Intro",
                                "type": "resource",
                                "url": "https://training.example/mod/resource/view.php?id=5222",
                            }
                        ],
                    }
                ]

        catalog = scan_remote(FakeClient(), self.public, [51])

        canonical = self.content / "004--001--5222"
        self.assertTrue((canonical / "files/Intro01.mp4").is_file())
        self.assertFalse(old.exists())
        self.assertEqual(catalog["stats"]["downloaded_files"], 1)
        manifest = json.loads((self.public / "courses/51/manifest.json").read_text())
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["local_url"], "courses/51/content/004--001--5222/files/Intro01.mp4")


if __name__ == "__main__":
    unittest.main()
