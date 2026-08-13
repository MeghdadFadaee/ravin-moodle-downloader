"""Import or update local exam questions and their attachments."""

from __future__ import annotations

import mimetypes
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from .local_files import atomic_copy
from .models import MoodleError
from .paths import _activity_directory_name, _clean_name, _read_json_object
from .scan import _atomic_json, _manifest_lock, scan_offline
from .wizard import prompt, prompt_path, select_number


@dataclass(frozen=True)
class QuestionsImportResult:
    course_id: int
    activity_id: int
    activity_key: str
    activity_title: str
    questions: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["files"] = list(self.files)
        return value


def _course_choices(public: Path) -> list[tuple[int, str, dict[str, Any]]]:
    choices: list[tuple[int, str, dict[str, Any]]] = []
    for manifest_path in sorted((public / "courses").glob("*/manifest.json")):
        manifest = _read_json_object(manifest_path)
        course = manifest.get("course")
        if not isinstance(course, dict):
            continue
        course_id = int(course.get("id") or 0)
        quizzes = _quiz_choices(course)
        if course_id and quizzes:
            title = str(course.get("fullname") or course.get("shortname") or f"Course {course_id}")
            choices.append((course_id, title, course))
    choices.sort(key=lambda value: value[0])
    return choices


def _quiz_choices(course: dict[str, Any]) -> list[tuple[int, str, str, str]]:
    choices: list[tuple[int, str, str, str]] = []
    for section in course.get("sections", []):
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("name") or f"Section {section.get('number', '')}")
        for item in section.get("items", []):
            if not isinstance(item, dict):
                continue
            if item.get("activity_type") != "quiz" and item.get("kind") != "assessment":
                continue
            activity_id = int(item.get("activity_id") or 0)
            if not activity_id:
                continue
            title = str(item.get("title") or f"Quiz {activity_id}")
            status = str(item.get("state", {}).get("questions") or "missing")
            choices.append((activity_id, title, section_title, status))
    return choices


def questions_wizard(
    public: Path,
    course_id: int | None = None,
    activity_id: int | None = None,
    questions_path: Path | None = None,
    attachment_paths: tuple[Path, ...] = (),
    *,
    prompt_for_attachment: bool = True,
    input_func: Callable[[str], str] = input,
    output: TextIO | None = None,
) -> tuple[int, int, Path, tuple[Path, ...]]:
    """Interactively fill omitted question-import arguments from local manifests."""
    public = public.expanduser().resolve()
    output = output or sys.stderr
    courses = _course_choices(public)
    if not courses:
        raise MoodleError(f"no local courses with quizzes found in {public / 'courses'}; run `ravin scan` first")

    courses_by_id = {choice[0]: choice for choice in courses}
    if course_id is None:
        print("\nCourses with exams:", file=output)
        for index, (candidate_id, title, course) in enumerate(courses, start=1):
            quiz_count = len(_quiz_choices(course))
            print(f"  [{index}] {title} (course {candidate_id}, {quiz_count} exam(s))", file=output)
        course_id = select_number(courses, "Select a course: ", input_func, output)
    if course_id not in courses_by_id:
        raise MoodleError(f"course {course_id} has no local quiz activities; run `ravin scan {course_id}` first")

    course = courses_by_id[course_id][2]
    quizzes = _quiz_choices(course)
    quiz_ids = {choice[0] for choice in quizzes}
    if activity_id is None:
        print("\nExams:", file=output)
        for index, (candidate_id, title, section_title, status) in enumerate(quizzes, start=1):
            print(
                f"  [{index}] {title} (activity {candidate_id}, section: {section_title}, questions: {status})",
                file=output,
            )
        activity_id = select_number(quizzes, "Select an exam: ", input_func, output)
    if activity_id not in quiz_ids:
        raise MoodleError(f"activity {activity_id} is not a quiz in course {course_id}")

    while questions_path is None:
        value = prompt(input_func, "Questions Markdown file: ")
        try:
            candidate = prompt_path(value)
            questions_path = _questions_source(candidate)
        except MoodleError as exc:
            print(f"Invalid questions file: {exc}", file=output)
    questions_path = _questions_source(questions_path)

    attachments = _attachment_sources(attachment_paths)
    if prompt_for_attachment and not attachments:
        while True:
            value = prompt(input_func, "Exam PDF (optional; press Enter to skip): ")
            if not value:
                break
            try:
                candidate = prompt_path(value)
                attachments = _attachment_sources((candidate,))
                break
            except MoodleError as exc:
                print(f"Invalid exam file: {exc}", file=output)

    return course_id, activity_id, questions_path, attachments


def _questions_source(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise MoodleError(f"questions file not found: {source}")
    if source.suffix.casefold() not in {".md", ".markdown"}:
        raise MoodleError("questions must be supplied as a Markdown file")
    try:
        content = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MoodleError("questions Markdown must be UTF-8 encoded") from exc
    if not content.strip():
        raise MoodleError("questions Markdown is empty")
    return source


def _attachment_sources(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    sources: list[Path] = []
    names: set[str] = set()
    for path in paths:
        source = path.expanduser().resolve()
        if not source.is_file():
            raise MoodleError(f"exam attachment not found: {source}")
        name = _clean_name(source.name, "exam-file")
        folded = name.casefold()
        if folded in names:
            raise MoodleError(f"multiple exam attachments resolve to the same filename: {name}")
        names.add(folded)
        sources.append(source)
    return tuple(sources)


def _find_activity(course: dict[str, Any], activity_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for section in course.get("sections", []):
        if not isinstance(section, dict):
            continue
        for item in section.get("items", []):
            if isinstance(item, dict) and int(item.get("activity_id") or 0) == activity_id:
                matches.append((section, item))
    if not matches:
        raise MoodleError(f"activity {activity_id} was not found in course {course.get('id')}; run `ravin scan` first")
    if len(matches) > 1:
        raise MoodleError(f"activity {activity_id} appears more than once in the course manifest")
    section, item = matches[0]
    if item.get("activity_type") != "quiz" and item.get("kind") != "assessment":
        raise MoodleError(f"activity {activity_id} is not a quiz or assessment")
    return section, item


def import_questions(
    public: Path,
    course_id: int,
    activity_id: int,
    questions_path: Path,
    attachment_paths: tuple[Path, ...] = (),
) -> QuestionsImportResult:
    """Place questions in their manifest-resolved bundle and reconcile the library."""
    public = public.expanduser().resolve()
    questions_source = _questions_source(questions_path)
    attachment_sources = _attachment_sources(attachment_paths)
    manifest_path = public / "courses" / str(course_id) / "manifest.json"
    manifest = _read_json_object(manifest_path)
    course = manifest.get("course")
    if not isinstance(course, dict):
        raise MoodleError(f"course manifest not found: {manifest_path}; run `ravin scan {course_id}` first")
    section, item = _find_activity(course, activity_id)
    key = str(item.get("key") or "") or _activity_directory_name(
        item.get("section_number", section.get("number")),
        item.get("activity_position"),
        activity_id,
    )
    activity_directory = public / "courses" / str(course_id) / "content" / key
    questions_destination = activity_directory / "artifacts" / "questions.fa.md"
    atomic_copy(questions_source, questions_destination)

    copied_files: list[str] = []
    for source in attachment_sources:
        filename = _clean_name(source.name, "exam-file")
        atomic_copy(source, activity_directory / "files" / filename)
        copied_files.append(filename)

    # Persist enough local information for an offline scan to expose the exam
    # and its primary attachment immediately. A later remote scan will merge the
    # same bundle through its stable activity ID.
    item["key"] = key
    item["bundle_path"] = f"content/{key}"
    item["kind"] = "assessment"
    if copied_files:
        primary = activity_directory / "files" / copied_files[0]
        item["filename"] = primary.name
        item["extension"] = primary.suffix.removeprefix(".").casefold()
        item["mimetype"] = mimetypes.guess_type(primary.name)[0] or "application/octet-stream"
        item.pop("remote_size", None)

    with _manifest_lock(public / "courses"):
        current = _read_json_object(manifest_path)
        current_course = current.get("course")
        if not isinstance(current_course, dict):
            raise MoodleError(f"course manifest changed while importing questions: {manifest_path}")
        _current_section, current_item = _find_activity(current_course, activity_id)
        current_item.update(
            {
                "key": key,
                "bundle_path": f"content/{key}",
                "kind": "assessment",
            }
        )
        if copied_files:
            primary = activity_directory / "files" / copied_files[0]
            current_item.update(
                {
                    "filename": primary.name,
                    "extension": primary.suffix.removeprefix(".").casefold(),
                    "mimetype": mimetypes.guess_type(primary.name)[0] or "application/octet-stream",
                }
            )
            current_item.pop("remote_size", None)
        current["course"] = current_course
        _atomic_json(manifest_path, current)

    scan_offline(public, [course_id])
    relative_questions = questions_destination.relative_to(public).as_posix()
    relative_files = tuple(
        (activity_directory / "files" / filename).relative_to(public).as_posix()
        for filename in copied_files
    )
    return QuestionsImportResult(
        course_id=course_id,
        activity_id=activity_id,
        activity_key=key,
        activity_title=str(item.get("title") or f"Activity {activity_id}"),
        questions=relative_questions,
        files=relative_files,
    )


def format_questions_result(result: QuestionsImportResult) -> str:
    file_count = len(result.files)
    suffix = f" and {file_count} attachment(s)" if file_count else ""
    return (
        f"Updated questions for {result.activity_title} "
        f"(course {result.course_id}, activity {result.activity_id}){suffix}."
    )
