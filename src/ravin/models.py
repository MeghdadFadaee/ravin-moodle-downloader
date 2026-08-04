"""Core value objects and expected errors."""

from dataclasses import dataclass


class MoodleError(RuntimeError):
    """A friendly, expected Moodle/API error."""


@dataclass(frozen=True)
class Course:
    id: int
    fullname: str
    shortname: str = ""


@dataclass(frozen=True)
class FileItem:
    course_id: int
    section: str
    activity: str
    filename: str
    url: str
    mimetype: str = ""
    filesize: int | None = None
    chapter: str = ""
    section_id: int | None = None
    section_number: int | None = None
    activity_id: int | None = None
    activity_type: str = ""
    activity_position: int | None = None
    description: str = ""
