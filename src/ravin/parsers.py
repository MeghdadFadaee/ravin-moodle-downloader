"""Small HTML and Moodle-page parsers."""

import html
import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._form = {
                "action": values.get("action", ""),
                "method": values.get("method", "get").lower(),
                "inputs": {},
            }
        elif tag == "input" and self._form is not None:
            name = values.get("name")
            if name:
                self._form["inputs"][name] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


class _LinkParser(HTMLParser):
    """Collect links and embedded media without keeping the whole DOM."""

    MEDIA_ATTRIBUTES = {
        "source": "src",
        "video": "src",
        "audio": "src",
        "track": "src",
        "embed": "src",
        "iframe": "src",
        "object": "data",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self.media: list[str] = []
        self.title = ""
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._in_title = False
        self._title_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self._anchor_href = urllib.parse.urljoin(self.base_url, values["href"] or "")
            self._anchor_text = []
        if tag == "title":
            self._in_title = True
        media_attr = self.MEDIA_ATTRIBUTES.get(tag)
        if media_attr and values.get(media_attr):
            self.media.append(urllib.parse.urljoin(self.base_url, values[media_attr] or ""))

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._in_title:
            self._title_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            label = " ".join("".join(self._anchor_text).split())
            self.links.append((self._anchor_href, label))
            self._anchor_href = None
            self._anchor_text = []
        if tag == "title":
            self._in_title = False
            self.title = " ".join("".join(self._title_text).split())


class _CourseStructureParser(HTMLParser):
    """Collect Moodle sections and activities from a course page."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.sections: list[dict[str, Any]] = []
        self._section: dict[str, Any] | None = None
        self._activity: dict[str, Any] | None = None
        self._markers: list[str | None] = []
        self._captures: list[tuple[int, dict[str, Any], str]] = []

    @staticmethod
    def _integer(value: str | None) -> int | None:
        try:
            return int(value or "")
        except ValueError:
            return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        marker: str | None = None
        if (
            tag == "li"
            and values.get("data-for") == "section"
            and (values.get("id") or "").startswith("section-")
        ):
            self._section = {
                "id": self._integer(values.get("data-id")),
                "number": self._integer(values.get("data-sectionid") or values.get("data-number")),
                "position": len(self.sections) + 1,
                "name": " ".join((values.get("data-sectionname") or "Section").split()),
                "summary": "",
                "activities": [],
            }
            self.sections.append(self._section)
            marker = "section"
        elif tag == "li" and self._section is not None and values.get("data-for") == "cmitem":
            module_type = next(
                (value.removeprefix("modtype_") for value in classes if value.startswith("modtype_")),
                "activity",
            )
            self._activity = {
                "id": self._integer(values.get("data-id")),
                "position": len(self._section["activities"]) + 1,
                "name": "",
                "type": module_type,
                "url": "",
                "description": "",
                "badge": "",
                "lms_completed": None,
            }
            self._section["activities"].append(self._activity)
            marker = "activity"

        if tag not in self.VOID_TAGS:
            self._markers.append(marker)
        depth = len(self._markers)
        if self._activity is not None:
            activity_name = values.get("data-activityname")
            if activity_name and not self._activity["name"]:
                self._activity["name"] = " ".join(html.unescape(activity_name).split())
            if tag == "a" and values.get("href") and "/mod/" in (values.get("href") or ""):
                self._activity["url"] = urllib.parse.urljoin(self.base_url, values["href"] or "")
            if "activity-description" in classes:
                self._captures.append((depth, self._activity, "description"))
            if "activitybadge" in classes:
                self._captures.append((depth, self._activity, "badge"))
            class_value = values.get("class") or ""
            if "btn-subtle-success" in class_value:
                self._activity["lms_completed"] = True
        if self._section is not None and "summary" in classes:
            self._captures.append((depth, self._section, "summary"))

    def handle_endtag(self, _tag: str) -> None:
        if _tag in self.VOID_TAGS:
            return
        depth = len(self._markers)
        self._captures = [capture for capture in self._captures if capture[0] != depth]
        if not self._markers:
            return
        marker = self._markers.pop()
        if marker == "activity":
            self._activity = None
        elif marker == "section":
            self._section = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        for _, target, field in self._captures:
            target[field] = f"{target.get(field, '')} {data}".strip()

    def result(self) -> list[dict[str, Any]]:
        for section in self.sections:
            section["summary"] = " ".join(section["summary"].split())
            for activity in section["activities"]:
                activity["name"] = " ".join((activity["name"] or activity["type"]).split())
                activity["description"] = " ".join(activity["description"].split())
                activity["badge"] = " ".join(activity["badge"].split())
        return self.sections


def _parse_moodle_config(page: str) -> dict[str, Any]:
    match = re.search(r"\bM\.cfg\s*=\s*", page)
    if not match:
        return {}
    start = page.find("{", match.end())
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(page)):
        character = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        return {}
    try:
        return json.loads(page[start:end])
    except json.JSONDecodeError:
        return {}
