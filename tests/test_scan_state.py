from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.scan import scan_offline, scan_remote
from ravin.catalog import _build_library_catalog
from ravin.models import Course


class QuizVisibilityTests(unittest.TestCase):
    def test_remote_scan_includes_quizzes_without_local_artifacts_as_assessments(self):
        class Client:
            site = "https://training.example"

            def list_courses(self):
                return [Course(56, "MCSA", "MCSA")]

            def list_files(self, course_id):
                return []

            def course_structure(self, course_id):
                return [{"number": 14, "name": "Practices", "activities": [
                    {"id": activity_id, "position": position, "type": "quiz", "name": f"Practice {position}"}
                    for position, activity_id in enumerate((6142, 6151, 6238), 1)
                ]}]

        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory)
            raw = _build_library_catalog(Client(), public)
            self.assertEqual([item["kind"] for item in raw["courses"][0]["sections"][0]["items"]],
                             ["assessment"] * 3)
            scan_remote(Client(), public)
            manifest = json.loads((public / "courses/56/manifest.json").read_text())
            items = manifest["course"]["sections"][0]["items"]
            self.assertEqual([item["activity_id"] for item in items], [6142, 6151, 6238])
            self.assertTrue(all(item["kind"] == "assessment" for item in items))
            self.assertTrue(all(item["state"]["questions"] == "missing" for item in items))

    def test_offline_scan_repairs_legacy_quiz_kind_and_preserves_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory)
            root = public / "courses/56"
            artifact = root / "content/014--001--6142/artifacts/questions.fa.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("# Practice three\n")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"course": {
                "id": 56, "fullname": "MCSA", "sections": [{"number": 14, "items": [
                    {"activity_id": activity_id, "activity_position": position,
                     "activity_type": "quiz", "kind": "quiz", "title": f"Practice {position}"}
                    for position, activity_id in enumerate((6142, 6151, 6238), 1)
                ]}]
            }}))
            scan_offline(public, [56])
            course = json.loads(manifest_path.read_text())["course"]
            items = course["sections"][0]["items"]
            self.assertTrue(all(item["kind"] == "assessment" for item in items))
            self.assertEqual([item["state"]["questions"] for item in items],
                             ["complete", "missing", "missing"])
            self.assertEqual(artifact.read_text(), "# Practice three\n")
            self.assertEqual(course["states"]["assessments"], {"total": 3, "complete": 1, "missing": 2})


class PortableArtifactStateTests(unittest.TestCase):
    def test_timestamp_rounding_does_not_stale_matching_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "public"
            activity = public / "courses" / "44" / "content" / "001--001--100"
            video = activity / "files" / "lesson.mp4"
            transcript = activity / "artifacts" / "transcript.fa.txt"
            video.parent.mkdir(parents=True)
            transcript.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            transcript.write_text("transcript", encoding="utf-8")
            video_stat = video.stat()
            transcript_stat = transcript.stat()
            rounded_video_mtime = video_stat.st_mtime_ns - 1_500_000_000
            rounded_transcript_mtime = transcript_stat.st_mtime_ns - 1_500_000_000
            (activity / "artifacts" / "transcript.meta.json").write_text(
                json.dumps(
                    {
                        "source": "../files/lesson.mp4",
                        "source_size": video_stat.st_size,
                        "source_mtime_ns": rounded_video_mtime,
                    }
                ),
                encoding="utf-8",
            )
            (activity / "artifacts" / "summary.fa.md").write_text("summary", encoding="utf-8")
            (activity / "artifacts" / "summary.meta.json").write_text(
                json.dumps(
                    {
                        "source": "transcript.fa.txt",
                        "source_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                        "source_size": transcript_stat.st_size,
                        "source_mtime_ns": rounded_transcript_mtime,
                    }
                ),
                encoding="utf-8",
            )
            manifest = public / "courses" / "44" / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "course": {
                            "id": 44,
                            "fullname": "Course",
                            "sections": [
                                {
                                    "number": 1,
                                    "items": [
                                        {
                                            "filename": "lesson.mp4",
                                            "activity_type": "resource",
                                            "kind": "video",
                                            "title": "Lesson",
                                            "section_number": 1,
                                            "activity_position": 1,
                                            "activity_id": 100,
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            scan_offline(public, [44])

            item = json.loads(manifest.read_text(encoding="utf-8"))["course"]["sections"][0]["items"][0]
            self.assertEqual(item["state"]["transcript"], "complete")
            self.assertEqual(item["state"]["summary"], "complete")


if __name__ == "__main__":
    unittest.main()
