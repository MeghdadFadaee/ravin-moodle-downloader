from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.models import MoodleError
from ravin.questions import import_questions
from ravin.scan import scan_offline


class CourseAssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.public = Path(self.temporary_directory.name) / "public"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_course(self, course_id: int, items: list[dict] | None = None) -> Path:
        course = self.public / "courses" / str(course_id)
        course.mkdir(parents=True)
        manifest = course / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "course": {
                        "id": course_id,
                        "fullname": f"Course {course_id}",
                        "source_url": f"https://training.example/course/view.php?id={course_id}",
                        "sections": [
                            {
                                "number": 1,
                                "name": "Course",
                                "items": items or [],
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        return course

    def write_overlay(self, course: Path, **assessment: object) -> None:
        (course / "assessments.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "assessments": [
                        {
                            "id": "final-exam",
                            "type": "final",
                            "title": "Final exam",
                            "status": "ended",
                            **assessment,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_local_exam_is_merged_and_survives_repeated_scans(self) -> None:
        course = self.write_course(56)
        self.write_overlay(course)
        questions = course / "assessments" / "final-exam" / "artifacts" / "questions.fa.md"
        questions.parent.mkdir(parents=True)
        questions.write_text("# Final exam", encoding="utf-8")

        scan_offline(self.public, [56])
        scan_offline(self.public, [56])

        manifest = json.loads((course / "manifest.json").read_text(encoding="utf-8"))
        items = [item for section in manifest["course"]["sections"] for item in section["items"]]
        exams = [item for item in items if item.get("assessment_id") == "final-exam"]
        self.assertEqual(len(exams), 1)
        self.assertEqual(exams[0]["state"]["questions"], "complete")
        self.assertEqual(exams[0]["bundle_path"], "assessments/final-exam")
        self.assertEqual(manifest["course"]["states"]["assessments"], {"complete": 1, "total": 1})

    def test_overlay_can_link_to_a_moodle_quiz(self) -> None:
        course = self.write_course(
            53,
            [
                {
                    "id": "activity-6126",
                    "activity_id": 6126,
                    "activity_position": 1,
                    "activity_type": "quiz",
                    "title": "Moodle quiz",
                    "kind": "quiz",
                    "filename": "",
                }
            ],
        )
        self.write_overlay(course, activity_id=6126)

        scan_offline(self.public, [53])

        manifest = json.loads((course / "manifest.json").read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][0]["items"][0]
        self.assertEqual(item["assessment_id"], "final-exam")
        self.assertEqual(item["activity_id"], 6126)
        self.assertEqual(item["kind"], "assessment")
        self.assertEqual(item["bundle_path"], "assessments/final-exam")

    def test_question_import_uses_the_local_assessment_bundle(self) -> None:
        course = self.write_course(51)
        self.write_overlay(course)
        scan_offline(self.public, [51])
        source = Path(self.temporary_directory.name) / "exam.md"
        source.write_text("# Final exam", encoding="utf-8")

        result = import_questions(self.public, 51, "final-exam", source)

        questions = course / "assessments" / "final-exam" / "artifacts" / "questions.fa.md"
        self.assertEqual(questions.read_text(encoding="utf-8"), "# Final exam")
        self.assertEqual(result.activity_key, "assessment--final-exam")
        self.assertEqual(
            result.questions,
            "courses/51/assessments/final-exam/artifacts/questions.fa.md",
        )
        manifest = json.loads((course / "manifest.json").read_text(encoding="utf-8"))
        item = manifest["course"]["sections"][-1]["items"][0]
        self.assertEqual(item["bundle_path"], "assessments/final-exam")
        self.assertEqual(item["state"]["questions"], "complete")

    def test_active_exam_is_visible_but_cannot_publish_answers(self) -> None:
        course = self.write_course(56)
        self.write_overlay(course, status="active")
        misplaced = course / "assessments" / "final-exam" / "artifacts" / "questions.fa.md"
        misplaced.parent.mkdir(parents=True)
        misplaced.write_text("# Must remain hidden", encoding="utf-8")
        scan_offline(self.public, [56])
        source = Path(self.temporary_directory.name) / "answers.md"
        source.write_text("# Answers", encoding="utf-8")

        with self.assertRaisesRegex(MoodleError, "publish questions only after it has ended"):
            import_questions(self.public, 56, "final-exam", source)

        manifest = json.loads((course / "manifest.json").read_text(encoding="utf-8"))
        states = manifest["course"]["states"]["assessments"]
        self.assertEqual(states, {"total": 1, "complete": 0, "active": 1})
        item = manifest["course"]["sections"][-1]["items"][0]
        self.assertNotIn("questions", item["artifacts"])


if __name__ == "__main__":
    unittest.main()
