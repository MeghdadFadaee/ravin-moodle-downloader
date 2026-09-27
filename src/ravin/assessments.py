"""Course-level assessment overlays that survive LMS scans."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import MoodleError
from .paths import _read_json_object


ASSESSMENT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ASSESSMENT_STATUSES = {"upcoming", "active", "ended", "unavailable"}


def assessment_directory(public: Path, course_id: int, assessment_id: str) -> Path:
    if not ASSESSMENT_ID_PATTERN.fullmatch(assessment_id):
        raise MoodleError(f"invalid assessment ID: {assessment_id!r}")
    return public / "courses" / str(course_id) / "assessments" / assessment_id


def load_assessments(public: Path, course_id: int) -> list[dict[str, Any]]:
    path = public / "courses" / str(course_id) / "assessments.json"
    if not path.exists():
        return []
    payload = _read_json_object(path)
    raw_assessments = payload.get("assessments")
    if not isinstance(raw_assessments, list):
        raise MoodleError(f"invalid assessments file: {path}")

    assessments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_assessments:
        if not isinstance(raw, dict):
            raise MoodleError(f"invalid assessment entry in {path}")
        assessment_id = str(raw.get("id") or "")
        if not ASSESSMENT_ID_PATTERN.fullmatch(assessment_id):
            raise MoodleError(f"invalid assessment ID in {path}: {assessment_id!r}")
        if assessment_id in seen:
            raise MoodleError(f"duplicate assessment ID in {path}: {assessment_id}")
        seen.add(assessment_id)
        status = str(raw.get("status") or "unavailable")
        if status not in ASSESSMENT_STATUSES:
            raise MoodleError(f"invalid status for assessment {assessment_id}: {status!r}")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise MoodleError(f"assessment {assessment_id} has no title in {path}")
        assessment = dict(raw)
        assessment.update({"id": assessment_id, "title": title, "status": status})
        assessments.append(assessment)
    return assessments


def _remove_previous_overlay(course: dict[str, Any]) -> None:
    retained_sections: list[dict[str, Any]] = []
    for section in course.get("sections", []):
        if not isinstance(section, dict):
            continue
        if section.get("assessment_source") == "overlay":
            continue
        retained_items: list[dict[str, Any]] = []
        for item in section.get("items", []):
            if not isinstance(item, dict):
                continue
            if item.get("assessment_source") == "overlay":
                item = dict(item)
                for key in (
                    "assessment_id", "assessment_type", "assessment_status", "assessment_source",
                ):
                    item.pop(key, None)
                if item.get("activity_type") == "quiz":
                    item["kind"] = "quiz"
            retained_items.append(item)
        section["items"] = retained_items
        retained_sections.append(section)
    course["sections"] = retained_sections


def merge_assessments(public: Path, course: dict[str, Any]) -> dict[str, Any]:
    """Merge a course's durable local assessment metadata into its activity tree."""
    course_id = int(course.get("id") or 0)
    if course_id <= 0:
        return course
    _remove_previous_overlay(course)
    assessments = load_assessments(public, course_id)
    if not assessments:
        return course

    sections = course.setdefault("sections", [])
    synthetic_items: list[dict[str, Any]] = []
    for position, assessment in enumerate(assessments, start=1):
        linked: dict[str, Any] | None = None
        activity_id = assessment.get("activity_id")
        if activity_id is not None:
            try:
                resolved_activity_id = int(activity_id)
            except (TypeError, ValueError) as exc:
                raise MoodleError(
                    f"invalid activity ID for assessment {assessment['id']}: {activity_id!r}"
                ) from exc
            for section in sections:
                for item in section.get("items", []):
                    if int(item.get("activity_id") or 0) == resolved_activity_id:
                        linked = item
                        break
                if linked is not None:
                    break
            if linked is None:
                raise MoodleError(
                    f"assessment {assessment['id']} links to missing activity {resolved_activity_id} "
                    f"in course {course_id}"
                )

        common = {
            "assessment_id": assessment["id"],
            "assessment_type": str(assessment.get("type") or "exam"),
            "assessment_status": assessment["status"],
            "assessment_source": "overlay",
            "kind": "assessment",
            "badge": str(assessment.get("badge") or "Final exam"),
        }
        if linked is not None:
            linked.update(common)
            linked["title"] = assessment["title"]
            if assessment.get("description") is not None:
                linked["description"] = str(assessment["description"])
            continue

        synthetic_items.append(
            {
                "id": f"assessment-{assessment['id']}",
                "section": "Exams",
                "section_id": None,
                "section_number": None,
                "activity_id": None,
                "activity_position": position,
                "activity_type": "assessment",
                "title": assessment["title"],
                "description": str(assessment.get("description") or ""),
                "filename": "",
                "extension": "",
                "mimetype": "",
                "source_url": course.get("source_url"),
                "lms_completed": None,
                **common,
            }
        )

    if synthetic_items:
        sections.append(
            {
                "id": "local-assessments",
                "number": None,
                "position": len(sections) + 1,
                "name": "Exams",
                "summary": "",
                "activity_count": len(synthetic_items),
                "assessment_source": "overlay",
                "items": synthetic_items,
            }
        )
    course["section_count"] = len(sections)
    course["activity_count"] = sum(len(section.get("items", [])) for section in sections)
    return course
