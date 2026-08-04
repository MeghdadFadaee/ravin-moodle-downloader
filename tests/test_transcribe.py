from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.transcribe import CourseTranscriber, TranscriptionOptions


class FakeModel:
    device = "test-device"

    def __init__(self, failures: set[str] | None = None, *, interrupt: bool = False) -> None:
        self.failures = failures or set()
        self.interrupt = interrupt
        self.calls: list[str] = []

    def transcribe(self, source: str, **_kwargs: object) -> dict[str, str]:
        name = Path(source).name
        self.calls.append(name)
        if self.interrupt:
            raise KeyboardInterrupt
        if name in self.failures:
            raise RuntimeError(f"cannot decode {name}")
        return {"text": f"transcript for {name}"}


class TranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.public = self.root / "public"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_course(self, filenames: list[str]) -> None:
        items = []
        for position, filename in enumerate(filenames, start=1):
            activity_id = 4900 + position
            key = f"002--{position:03d}--{activity_id}"
            source = self.public / "courses" / "44" / "content" / key / "files" / filename
            source.parent.mkdir(parents=True)
            source.write_bytes(filename.encode("utf-8"))
            items.append(
                {
                    "filename": filename,
                    "kind": "video",
                    "activity_type": "resource",
                    "title": f"Lesson {position}",
                    "section_number": 2,
                    "activity_position": position,
                    "activity_id": activity_id,
                    "key": key,
                    "bundle_path": f"content/{key}",
                }
            )
        manifest_path = self.public / "courses" / "44" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "course": {
                        "id": 44,
                        "fullname": "Network +",
                        "section_count": 1,
                        "activity_count": len(items),
                        "sections": [{"number": 2, "name": "Lessons", "items": items}],
                    },
                }
            ),
            encoding="utf-8",
        )

    def options(self, **overrides: object) -> TranscriptionOptions:
        values: dict[str, object] = {
            "public": self.public,
            "course_ids": (44,),
            "model": "tiny",
            "device": "cpu",
            "language": "fa",
            "retries": 2,
            "keep_awake": False,
        }
        values.update(overrides)
        return TranscriptionOptions(**values)

    def transcriber(self, model: FakeModel, **overrides: object) -> CourseTranscriber:
        return CourseTranscriber(
            self.options(**overrides),
            model_loader=lambda _name, _device: (model, None, "cpu"),
            media_validator=lambda _source: None,
            sleeper=lambda _delay: None,
        )

    def test_success_updates_artifacts_manifest_and_resumes(self) -> None:
        self.write_course(["lesson.mp4"])
        model = FakeModel()
        result = self.transcriber(model).run()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(model.calls, ["lesson.mp4"])
        artifacts = self.public / "courses" / "44" / "content" / "002--001--4901" / "artifacts"
        transcript = artifacts / "transcript.fa.txt"
        metadata = json.loads((artifacts / "transcript.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(transcript.read_text(encoding="utf-8"), "transcript for lesson.mp4\n")
        self.assertEqual(metadata["source"], "../files/lesson.mp4")
        self.assertEqual(metadata["transcript"], "transcript.fa.txt")
        self.assertNotIn(str(self.root), json.dumps(metadata))
        self.assertEqual(os.stat(transcript).st_mode & 0o777, 0o644)
        manifest = json.loads(
            (self.public / "courses" / "44" / "manifest.json").read_text(encoding="utf-8")
        )
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["state"]["transcript"], "complete")
        self.assertTrue(item["transcribed"])

        second_loader_called = False

        def second_loader(_name: str, _device: str) -> tuple[object, object, str]:
            nonlocal second_loader_called
            second_loader_called = True
            raise AssertionError("matching transcripts must skip model loading")

        second = CourseTranscriber(
            self.options(),
            model_loader=second_loader,
            media_validator=lambda _source: None,
        ).run()
        self.assertEqual(second.skipped, 1)
        self.assertFalse(second_loader_called)

    def test_permanent_failure_is_retried_recorded_and_does_not_stop_batch(self) -> None:
        self.write_course(["bad.mp4", "good.mp4"])
        model = FakeModel({"bad.mp4"})
        result = self.transcriber(model).run()

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(model.calls.count("bad.mp4"), 3)
        self.assertEqual(model.calls.count("good.mp4"), 1)
        bad_artifacts = self.public / "courses" / "44" / "content" / "002--001--4901" / "artifacts"
        good_artifacts = self.public / "courses" / "44" / "content" / "002--002--4902" / "artifacts"
        self.assertTrue((bad_artifacts / ".transcript.error.json").is_file())
        self.assertFalse((bad_artifacts / "transcript.fa.txt").exists())
        self.assertTrue((good_artifacts / "transcript.fa.txt").is_file())
        manifest = json.loads(
            (self.public / "courses" / "44" / "manifest.json").read_text(encoding="utf-8")
        )
        bad_item, good_item = manifest["course"]["sections"][0]["items"]
        self.assertEqual(bad_item["state"]["transcript"], "error")
        self.assertEqual(good_item["state"]["transcript"], "complete")
        self.assertEqual(manifest["course"]["states"]["errors"], 1)
        self.assertTrue((self.root / ".ravin" / "transcribe" / "errors.jsonl").is_file())

    def test_keyboard_interrupt_preserves_state_for_resume(self) -> None:
        self.write_course(["lesson.mp4", "later.mp4"])
        model = FakeModel(interrupt=True)
        transcriber = self.transcriber(model)
        result = transcriber.run()

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(model.calls, ["lesson.mp4"])
        state = json.loads(transcriber.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "interrupted")

    def test_dry_run_does_not_load_model_or_write_runtime_state(self) -> None:
        self.write_course(["lesson.mp4"])

        def forbidden_loader(_name: str, _device: str) -> tuple[object, object, str]:
            raise AssertionError("dry-run must not load Whisper")

        transcriber = CourseTranscriber(
            self.options(dry_run=True),
            model_loader=forbidden_loader,
            media_validator=lambda _source: None,
        )
        result = transcriber.run()

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.total, 1)
        self.assertFalse(transcriber.runtime_root.exists())


if __name__ == "__main__":
    unittest.main()
