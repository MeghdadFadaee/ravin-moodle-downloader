from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin import Course
from ravin.models import MoodleError
from ravin.recordings import import_recording, recording_wizard
from ravin.scan import scan_remote
from ravin.summarize import CourseSummarizer, SummaryOptions
from ravin.transcribe import CourseTranscriber, TranscriptionOptions


class FakeModel:
    device = "cpu"

    def transcribe(self, source: str, **_kwargs: object) -> dict[str, str]:
        return {"text": f"transcript for {Path(source).name}"}


class RecordingImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.public = self.root / "public"
        self.manifest_path = self.public / "courses" / "44" / "manifest.json"
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "course": {
                        "id": 44,
                        "fullname": "Network +",
                        "sections": [
                            {
                                "number": 27,
                                "name": "Live support classes",
                                "items": [
                                    {
                                        "activity_id": 5097,
                                        "activity_position": 1,
                                        "activity_type": "bigbluebuttonbn",
                                        "title": "Group A live class",
                                        "kind": "live class",
                                        "key": "027--001--5097",
                                        "bundle_path": "content/027--001--5097",
                                        "filename": "",
                                    }
                                ],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.video = self.root / "class recording.mp4"
        self.video.write_bytes(b"synthetic recording")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_wizard_lists_courses_and_live_classes(self) -> None:
        answers = iter(["1", "1", str(self.video)])
        output = io.StringIO()

        values = recording_wizard(
            self.public,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        self.assertEqual(values, (44, 5097, self.video.resolve()))
        display = output.getvalue()
        self.assertIn("Courses with live classes:", display)
        self.assertIn("Network + (course 44, 1 live class(es))", display)
        self.assertIn("Group A live class (activity 5097", display)
        self.assertIn("recording: missing", display)

    def test_import_becomes_transcribable_and_summarizable_media(self) -> None:
        result = import_recording(self.public, 44, 5097, self.video)

        bundle = self.public / "courses" / "44" / "content" / "027--001--5097"
        recording = bundle / "files" / "class recording.mp4"
        self.assertEqual(recording.read_bytes(), b"synthetic recording")
        self.assertEqual(result.recording, "courses/44/content/027--001--5097/files/class recording.mp4")
        metadata = json.loads((bundle / "artifacts" / "recording.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["source"], "../files/class recording.mp4")
        self.assertNotIn(str(self.root), json.dumps(metadata))

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["kind"], "video")
        self.assertEqual(item["state"]["download"], "complete")
        self.assertEqual(item["state"]["transcript"], "missing")
        self.assertEqual(item["state"]["summary"], "missing")
        self.assertEqual(item["file_versions"][0]["state"], "current")

        transcriber = CourseTranscriber(
            TranscriptionOptions(
                public=self.public,
                course_ids=(44,),
                model="tiny",
                device="cpu",
                keep_awake=False,
            ),
            model_loader=lambda _name, _device: (FakeModel(), None, "cpu"),
            media_validator=lambda _source: None,
        )
        transcribed = transcriber.run()
        self.assertEqual(transcribed.succeeded, 1)

        summary = CourseSummarizer(
            SummaryOptions(public=self.public, course_ids=(44,), dry_run=True, keep_awake=False)
        ).run()
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.unavailable, 0)

    def test_same_name_replacement_archives_previous_recording(self) -> None:
        import_recording(self.public, 44, 5097, self.video)
        replacement_directory = self.root / "replacement"
        replacement_directory.mkdir()
        replacement = replacement_directory / self.video.name
        replacement.write_bytes(b"replacement recording")

        result = import_recording(self.public, 44, 5097, replacement)

        bundle = self.public / "courses" / "44" / "content" / "027--001--5097"
        self.assertEqual((bundle / "files" / self.video.name).read_bytes(), b"replacement recording")
        archives = list((bundle / "files" / "archive").glob(f"*--{self.video.name}"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), b"synthetic recording")
        self.assertEqual(len(result.archived), 1)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        versions = manifest["course"]["sections"][0]["items"][0]["file_versions"]
        self.assertEqual([version["state"] for version in versions], ["current", "archived"])

    def test_online_scan_keeps_the_selected_live_class_recording(self) -> None:
        import_recording(self.public, 44, 5097, self.video)

        class FakeClient:
            site = "https://training.example"

            @staticmethod
            def list_courses() -> list[Course]:
                return [Course(44, "Network +")]

            @staticmethod
            def list_files(_course_id: int) -> list[object]:
                return []

            @staticmethod
            def course_structure(_course_id: int) -> list[dict[str, object]]:
                return [
                    {
                        "number": 27,
                        "name": "Live support classes",
                        "activities": [
                            {
                                "id": 5097,
                                "position": 1,
                                "type": "bigbluebuttonbn",
                                "name": "Group A live class",
                            }
                        ],
                    }
                ]

        scan_remote(FakeClient(), self.public, [44])

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["filename"], "class recording.mp4")
        self.assertEqual(item["kind"], "video")
        self.assertEqual(item["state"]["download"], "complete")

    def test_rejects_non_video_and_non_live_activity(self) -> None:
        text_file = self.root / "notes.txt"
        text_file.write_text("not video", encoding="utf-8")
        with self.assertRaisesRegex(MoodleError, "must be a video file"):
            import_recording(self.public, 44, 5097, text_file)

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        item["activity_type"] = "forum"
        item["kind"] = "discussion"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MoodleError, "not a live class"):
            import_recording(self.public, 44, 5097, self.video)


if __name__ == "__main__":
    unittest.main()
