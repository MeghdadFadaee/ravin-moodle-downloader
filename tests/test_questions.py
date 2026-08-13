from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.models import MoodleError
from ravin.questions import import_questions, questions_wizard


class QuestionsImportTests(unittest.TestCase):
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
                                "number": 26,
                                "name": "Second exam",
                                "items": [
                                    {
                                        "activity_id": 5679,
                                        "activity_position": 1,
                                        "activity_type": "quiz",
                                        "title": "Second exam",
                                        "kind": "quiz",
                                        "key": "026--001--5679",
                                        "bundle_path": "content/026--001--5679",
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
        self.questions = self.root / "questions.fa.md"
        self.questions.write_text("# آزمون دوم\n\n## سؤال ۱\n\n**پاسخ صحیح: B**\n", encoding="utf-8")
        self.exam = self.root / "5679.pdf"
        self.exam.write_bytes(b"synthetic exam")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_import_places_questions_and_attachment_and_refreshes_manifest(self) -> None:
        result = import_questions(self.public, 44, 5679, self.questions, (self.exam,))

        bundle = self.public / "courses" / "44" / "content" / "026--001--5679"
        questions = bundle / "artifacts" / "questions.fa.md"
        attachment = bundle / "files" / "5679.pdf"
        self.assertEqual(questions.read_text(encoding="utf-8"), self.questions.read_text(encoding="utf-8"))
        self.assertEqual(attachment.read_bytes(), b"synthetic exam")
        self.assertEqual(os.stat(questions).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(attachment).st_mode & 0o777, 0o644)
        self.assertEqual(result.activity_key, "026--001--5679")
        self.assertEqual(result.files, ("courses/44/content/026--001--5679/files/5679.pdf",))

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["kind"], "assessment")
        self.assertEqual(item["filename"], "5679.pdf")
        self.assertEqual(item["mimetype"], "application/pdf")
        self.assertEqual(item["state"]["download"], "complete")
        self.assertEqual(item["state"]["questions"], "complete")
        self.assertEqual(item["artifacts"]["questions"]["path"], "artifacts/questions.fa.md")
        self.assertEqual(manifest["course"]["states"]["assessments"], {"complete": 1, "total": 1})

    def test_import_updates_existing_questions_without_requiring_attachment(self) -> None:
        import_questions(self.public, 44, 5679, self.questions, (self.exam,))
        self.questions.write_text("# نسخه جدید\n", encoding="utf-8")

        result = import_questions(self.public, 44, 5679, self.questions)

        bundle = self.public / "courses" / "44" / "content" / "026--001--5679"
        self.assertEqual((bundle / "artifacts" / "questions.fa.md").read_text(encoding="utf-8"), "# نسخه جدید\n")
        self.assertTrue((bundle / "files" / "5679.pdf").is_file())
        self.assertEqual(result.files, ())
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["state"]["questions"], "complete")
        self.assertEqual(item["state"]["download"], "complete")

    def test_rejects_unknown_or_non_quiz_activity(self) -> None:
        with self.assertRaisesRegex(MoodleError, "activity 9999 was not found"):
            import_questions(self.public, 44, 9999, self.questions)

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        item["activity_type"] = "resource"
        item["kind"] = "document"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MoodleError, "not a quiz or assessment"):
            import_questions(self.public, 44, 5679, self.questions)

    def test_wizard_lists_courses_and_exams_then_collects_files(self) -> None:
        answers = iter(["1", "1", str(self.questions), str(self.exam)])
        output = io.StringIO()

        values = questions_wizard(
            self.public,
            input_func=lambda _prompt: next(answers),
            output=output,
        )

        course_id, activity_id, questions, attachments = values
        self.assertEqual(course_id, 44)
        self.assertEqual(activity_id, 5679)
        self.assertEqual(questions, self.questions.resolve())
        self.assertEqual(attachments, (self.exam.resolve(),))
        display = output.getvalue()
        self.assertIn("Courses with exams:", display)
        self.assertIn("Network + (course 44, 1 exam(s))", display)
        self.assertIn("Second exam (activity 5679", display)
        self.assertIn("questions: missing", display)

    def test_wizard_accepts_ids_and_optional_empty_pdf(self) -> None:
        answers = iter(["44", "5679", str(self.questions), ""])

        values = questions_wizard(
            self.public,
            input_func=lambda _prompt: next(answers),
            output=io.StringIO(),
        )

        self.assertEqual(values, (44, 5679, self.questions.resolve(), ()))


if __name__ == "__main__":
    unittest.main()
