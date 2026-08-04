from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.summarize import CourseSummarizer, SummaryJob, SummaryOptions


class FakeCodex:
    def __init__(self, failures: set[str] | None = None, *, interrupt: bool = False) -> None:
        self.failures = failures or set()
        self.interrupt = interrupt
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, job: SummaryJob, prompt: str) -> str:
        self.calls.append(job.key)
        self.prompts.append(prompt)
        if self.interrupt:
            raise KeyboardInterrupt
        if job.key in self.failures:
            raise RuntimeError(f"cannot summarize {job.key}")
        return f"# خلاصه: {job.activity_title}\n\nخلاصه آزمایشی"


class SummarizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.public = self.root / "public"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_course(self, transcript_names: list[str], *, missing: int = 0) -> None:
        items = []
        total = len(transcript_names) + missing
        for position in range(1, total + 1):
            activity_id = 4900 + position
            key = f"002--{position:03d}--{activity_id}"
            items.append(
                {
                    "filename": f"lesson-{position}.mp4",
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
            if position <= len(transcript_names):
                transcript = self.public / "courses" / "44" / "content" / key / "artifacts" / "transcript.fa.txt"
                transcript.parent.mkdir(parents=True)
                transcript.write_text(transcript_names[position - 1], encoding="utf-8")
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
                        "activity_count": total,
                        "sections": [{"number": 2, "name": "Lessons", "items": items}],
                    },
                }
            ),
            encoding="utf-8",
        )

    def options(self, **overrides: object) -> SummaryOptions:
        values: dict[str, object] = {
            "public": self.public,
            "course_ids": (44,),
            "model": "test-model",
            "retries": 2,
            "timeout": 30,
            "keep_awake": False,
        }
        values.update(overrides)
        return SummaryOptions(**values)

    def summarizer(self, runner: FakeCodex, **overrides: object) -> CourseSummarizer:
        summarizer = CourseSummarizer(
            self.options(**overrides),
            codex_runner=runner,
            sleeper=lambda _delay: None,
        )
        summarizer._check_codex = lambda: setattr(summarizer, "codex_version", "codex-cli test")
        return summarizer

    def test_success_writes_portable_artifacts_updates_manifest_and_resumes(self) -> None:
        self.write_course(["متن درس شبکه"])
        runner = FakeCodex()
        result = self.summarizer(runner).run()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.succeeded, 1)
        self.assertIn("<transcript>\nمتن درس شبکه\n</transcript>", runner.prompts[0])
        artifacts = self.public / "courses" / "44" / "content" / "002--001--4901" / "artifacts"
        summary = artifacts / "summary.fa.md"
        metadata = json.loads((artifacts / "summary.meta.json").read_text(encoding="utf-8"))
        self.assertTrue(summary.read_text(encoding="utf-8").startswith("# خلاصه: Lesson 1"))
        self.assertEqual(metadata["source"], "transcript.fa.txt")
        self.assertEqual(metadata["generator"], "codex-cli")
        self.assertEqual(metadata["prompt_version"], 1)
        self.assertNotIn(str(self.root), json.dumps(metadata))
        self.assertEqual(os.stat(summary).st_mode & 0o777, 0o644)
        manifest = json.loads((self.public / "courses" / "44" / "manifest.json").read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["state"]["summary"], "complete")
        self.assertTrue(item["summarized"])

        second_runner = FakeCodex()
        second = self.summarizer(second_runner).run()
        self.assertEqual(second.skipped, 1)
        self.assertEqual(second_runner.calls, [])

    def test_failure_retries_records_error_and_continues(self) -> None:
        self.write_course(["bad transcript", "good transcript"])
        runner = FakeCodex({"002--001--4901"})
        result = self.summarizer(runner).run()

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(runner.calls.count("002--001--4901"), 3)
        self.assertEqual(runner.calls.count("002--002--4902"), 1)
        bad_artifacts = self.public / "courses" / "44" / "content" / "002--001--4901" / "artifacts"
        good_artifacts = self.public / "courses" / "44" / "content" / "002--002--4902" / "artifacts"
        self.assertTrue((bad_artifacts / ".summary.error.json").is_file())
        self.assertTrue((good_artifacts / "summary.fa.md").is_file())
        manifest = json.loads((self.public / "courses" / "44" / "manifest.json").read_text(encoding="utf-8"))
        bad_item, good_item = manifest["course"]["sections"][0]["items"]
        self.assertEqual(bad_item["state"]["summary"], "error")
        self.assertEqual(good_item["state"]["summary"], "complete")
        self.assertEqual(manifest["course"]["states"]["errors"], 1)
        self.assertTrue((self.root / ".ravin" / "summarize" / "errors.jsonl").is_file())

    def test_interrupt_preserves_state(self) -> None:
        self.write_course(["lesson"])
        summarizer = self.summarizer(FakeCodex(interrupt=True))
        result = summarizer.run()

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.exit_code, 130)
        state = json.loads(summarizer.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "interrupted")

    def test_dry_run_needs_no_codex_and_reports_missing_transcripts(self) -> None:
        self.write_course(["lesson"], missing=1)
        runner = FakeCodex()
        summarizer = CourseSummarizer(self.options(dry_run=True), codex_runner=runner)
        result = summarizer.run()

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.total, 1)
        self.assertEqual(result.unavailable, 1)
        self.assertEqual(runner.calls, [])
        self.assertFalse(summarizer.runtime_root.exists())

    def test_codex_exec_uses_ephemeral_read_only_isolated_workspace(self) -> None:
        self.write_course(["lesson"])
        commands: list[list[str]] = []
        working_directories: list[Path] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "codex-cli test\n", "")
            working_directory = Path(str(kwargs["cwd"]))
            working_directories.append(working_directory)
            self.assertNotEqual(working_directory, self.root)
            self.assertEqual(list(working_directory.iterdir()), [])
            self.assertIn("<transcript>\nlesson\n</transcript>", str(kwargs["input"]))
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("# خلاصه\n\nنتیجه", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch("ravin.summarize.shutil.which", return_value="/usr/local/bin/codex"),
            patch("ravin.summarize.subprocess.run", side_effect=fake_run),
        ):
            result = CourseSummarizer(self.options()).run()

        self.assertEqual(result.exit_code, 0)
        execution = commands[-1]
        self.assertIn("--ephemeral", execution)
        self.assertIn("--skip-git-repo-check", execution)
        self.assertEqual(execution[execution.index("--sandbox") + 1], "read-only")
        self.assertFalse(working_directories[0].exists())


if __name__ == "__main__":
    unittest.main()
