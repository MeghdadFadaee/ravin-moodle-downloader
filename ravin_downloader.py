#!/usr/bin/env python3
"""List and download files from an enrolled Moodle course.

The client prefers Moodle's documented mobile web-service API.  If that
service is disabled by the site administrator, it falls back to an ordinary
web login plus the same AJAX endpoint used by Moodle's "My courses" page.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import functools
import getpass
import hashlib
import html
import http.cookiejar
import http.client
import http.server
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SITE = "https://training.ravinacademy.com"
DEFAULT_RAVIN_LOGIN_URL = "https://lms.ravinacademy.com/"
__version__ = "0.3.0"
USER_AGENT = f"RavinMoodleDownloader/{__version__} (+personal Moodle client)"
DOWNLOADABLE_MODULES = {"resource", "folder", "page", "book"}
DEFAULT_ENV_FILE = Path.cwd() / ".env"
ENV_KEYS = {
    "username": "RAVIN_USERNAME",
    "password": "RAVIN_PASSWORD",
    "login_url": "RAVIN_LOGIN_URL",
    "user_agent": "RAVIN_USER_AGENT",
    "cookie": "RAVIN_COOKIE",
}
LIBRARY_ASSETS = ("index.html", "course.html", "styles.css", "app.js")


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


def _clean_name(value: str, fallback: str = "item") -> str:
    value = html.unescape(value).strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def _filename_from_headers(headers: Any, url: str, fallback: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    star = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.I)
    normal = re.search(r'filename="?([^";]+)', disposition, flags=re.I)
    if star:
        name = urllib.parse.unquote(star.group(1))
    elif normal:
        name = normal.group(1).strip()
    else:
        name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    if not name:
        content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
        extension = mimetypes.guess_extension(content_type) or ""
        name = fallback + extension
    return _clean_name(name, fallback)


def _unique(items: Iterable[FileItem]) -> list[FileItem]:
    result: list[FileItem] = []
    seen: set[str] = set()
    for item in items:
        key = urllib.parse.urlsplit(item.url)._replace(query="", fragment="").geturl()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _is_cloudflare_challenge(page: str) -> bool:
    sample = page[:20000].casefold()
    return any(
        marker in sample
        for marker in (
            "/cdn-cgi/",
            "cf-browser-verification",
            "challenge-platform",
            "enable javascript and cookies to continue",
        )
    )


class MoodleClient:
    def __init__(
        self,
        site: str,
        username: str = "",
        password: str = "",
        web_only: bool = False,
        cookie_header: str = "",
        browser_user_agent: str = "",
    ) -> None:
        self.site = site.rstrip("/")
        self.username = username
        self.password = password
        self.web_only = web_only
        self.user_agent = browser_user_agent or USER_AGENT
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        if cookie_header:
            self._install_cookie_header(cookie_header)
        self.has_browser_session = bool(cookie_header)
        self.token: str | None = None
        self.user_id: int | None = None
        self.sesskey: str | None = None
        self.mode = ""
        self._course_structure_cache: dict[int, list[dict[str, Any]]] = {}

    def _install_cookie_header(self, cookie_header: str) -> None:
        parsed_site = urllib.parse.urlparse(self.site)
        hostname = parsed_site.hostname
        if not hostname:
            raise MoodleError(f"invalid Moodle site URL: {self.site}")
        installed = 0
        for pair in cookie_header.split(";"):
            if "=" not in pair:
                continue
            name, value = pair.strip().split("=", 1)
            if not name:
                continue
            cookie = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=hostname,
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=parsed_site.scheme == "https",
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None},
                rfc2109=False,
            )
            self.cookies.set_cookie(cookie)
            installed += 1
        if not installed:
            raise MoodleError("the pasted Cookie header did not contain any cookies")

    def _request(
        self,
        path_or_url: str,
        *,
        data: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 45,
    ) -> Any:
        url = urllib.parse.urljoin(self.site + "/", path_or_url)
        body: bytes | None
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data, doseq=True).encode()
        else:
            body = data
        request_headers = {"User-Agent": self.user_agent, "Accept-Language": "en,fa;q=0.8"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace").strip()
            raise MoodleError(f"HTTP {exc.code} from {url}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MoodleError(f"Could not connect to {url}: {exc.reason}") from exc
        except (TimeoutError, http.client.HTTPException) as exc:
            raise MoodleError(f"connection to {url} failed: {exc}") from exc

    def _read_text(self, path_or_url: str, **kwargs: Any) -> tuple[str, str, Any]:
        with self._request(path_or_url, **kwargs) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "replace"), response.geturl(), response.headers

    def authenticate(self) -> str:
        if self.has_browser_session:
            self._login_browser_session()
            self.mode = "browser-session"
            return self.mode
        mobile_error = ""
        if not self.web_only:
            try:
                self._login_mobile_api()
                self.mode = "mobile-api"
                return self.mode
            except MoodleError as exc:
                mobile_error = str(exc)

        try:
            self._login_web()
            self.mode = "web-session"
            return self.mode
        except MoodleError as exc:
            if mobile_error:
                raise MoodleError(
                    f"Mobile API login was unavailable ({mobile_error}); "
                    f"web login also failed ({exc})."
                ) from exc
            raise

    def _login_mobile_api(self) -> None:
        text, _, _ = self._read_text(
            "/login/token.php",
            data={
                "username": self.username,
                "password": self.password,
                "service": "moodle_mobile_app",
            },
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            if _is_cloudflare_challenge(text):
                raise MoodleError("Cloudflare requires a real browser session; use --browser-session") from exc
            raise MoodleError("the token endpoint did not return JSON") from exc
        token = payload.get("token")
        if not token:
            message = payload.get("error") or payload.get("message") or "token service rejected the login"
            raise MoodleError(str(message))
        self.token = str(token)
        site_info = self.api_call("core_webservice_get_site_info")
        self.user_id = int(site_info["userid"])

    def _login_web(self) -> None:
        login_page, login_url, _ = self._read_text("/login/index.php")
        if _is_cloudflare_challenge(login_page):
            raise MoodleError("Cloudflare requires a real browser session; use --browser-session")
        parser = _FormParser()
        parser.feed(login_page)
        form = next(
            (form for form in parser.forms if "username" in form["inputs"] and "password" in form["inputs"]),
            None,
        )
        if form is None:
            raise MoodleError("could not find Moodle's login form")
        fields = dict(form["inputs"])
        fields.update({"username": self.username, "password": self.password})
        action = urllib.parse.urljoin(login_url, form["action"] or "/login/index.php")
        result, final_url, _ = self._read_text(action, data=fields)
        result_parser = _FormParser()
        result_parser.feed(result)
        still_has_login = any(
            "username" in item["inputs"] and "password" in item["inputs"]
            for item in result_parser.forms
        )
        if still_has_login or "/login/index.php" in urllib.parse.urlparse(final_url).path:
            error_match = re.search(
                r'class="[^"]*(?:loginerrors|alert-danger)[^"]*"[^>]*>(.*?)</',
                result,
                flags=re.I | re.S,
            )
            detail = re.sub(r"<[^>]+>", " ", error_match.group(1)) if error_match else "login rejected"
            raise MoodleError(" ".join(html.unescape(detail).split()))
        config = _parse_moodle_config(result)
        self.sesskey = str(config.get("sesskey") or "") or None
        if config.get("userId") is not None:
            self.user_id = int(config["userId"])
        if not self.sesskey:
            dashboard, _, _ = self._read_text("/my/courses.php")
            config = _parse_moodle_config(dashboard)
            self.sesskey = str(config.get("sesskey") or "") or None
            if config.get("userId") is not None:
                self.user_id = int(config["userId"])
        if not self.sesskey:
            raise MoodleError("login succeeded, but Moodle's session key was not found")

    def _login_browser_session(self) -> None:
        dashboard, final_url, _ = self._read_text("/my/courses.php")
        if _is_cloudflare_challenge(dashboard):
            raise MoodleError(
                "Cloudflare rejected the browser session. Copy a fresh Cookie header and use the exact same User-Agent."
            )
        parser = _FormParser()
        parser.feed(dashboard)
        login_form_present = any(
            "username" in item["inputs"] and "password" in item["inputs"]
            for item in parser.forms
        )
        if login_form_present or "/login/index.php" in urllib.parse.urlparse(final_url).path:
            raise MoodleError("the pasted browser session is expired or is not logged in")
        config = _parse_moodle_config(dashboard)
        self.sesskey = str(config.get("sesskey") or "") or None
        if config.get("userId") is not None:
            self.user_id = int(config["userId"])
        if not self.sesskey:
            raise MoodleError("the page opened, but Moodle's session key was not found")

    def api_call(self, function: str, **params: Any) -> Any:
        if not self.token:
            raise MoodleError("the mobile API is not authenticated")
        fields = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            **params,
        }
        text, _, _ = self._read_text("/webservice/rest/server.php", data=fields)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MoodleError(f"{function} returned invalid JSON") from exc
        if isinstance(payload, dict) and (payload.get("exception") or payload.get("errorcode")):
            raise MoodleError(str(payload.get("message") or payload.get("errorcode")))
        return payload

    def ajax_call(self, function: str, args: dict[str, Any]) -> Any:
        if not self.sesskey:
            raise MoodleError("the web session is not authenticated")
        body = json.dumps([{"index": 0, "methodname": function, "args": args}]).encode()
        text, _, _ = self._read_text(
            f"/lib/ajax/service.php?sesskey={urllib.parse.quote(self.sesskey)}&info={urllib.parse.quote(function)}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MoodleError(f"{function} returned invalid JSON") from exc
        if not isinstance(payload, list) or not payload:
            raise MoodleError(f"{function} returned an unexpected response")
        result = payload[0]
        if result.get("error"):
            exception = result.get("exception") or {}
            raise MoodleError(str(exception.get("message") or exception.get("errorcode") or "AJAX error"))
        return result.get("data")

    def list_courses(self) -> list[Course]:
        if self.token:
            if self.user_id is None:
                raise MoodleError("Moodle did not return the current user ID")
            payload = self.api_call("core_enrol_get_users_courses", userid=self.user_id)
            courses = [
                Course(int(item["id"]), str(item.get("fullname") or item.get("displayname") or item["id"]), str(item.get("shortname") or ""))
                for item in payload
            ]
        else:
            data = self.ajax_call(
                "core_course_get_enrolled_courses_by_timeline_classification",
                {
                    "classification": "all",
                    "limit": 0,
                    "offset": 0,
                    "sort": "fullname",
                    "customfieldname": "",
                    "customfieldvalue": "",
                    "requiredfields": ["id", "fullname", "shortname", "visible", "enddate"],
                },
            )
            raw_courses = data.get("courses", []) if isinstance(data, dict) else []
            courses = [
                Course(int(item["id"]), str(item.get("fullname") or item.get("displayname") or item["id"]), str(item.get("shortname") or ""))
                for item in raw_courses
            ]
        return sorted(courses, key=lambda course: course.fullname.casefold())

    def list_files(self, course_id: int) -> list[FileItem]:
        if self.token:
            try:
                return self._list_files_api(course_id)
            except MoodleError as exc:
                print(f"warning: course-content API unavailable ({exc}); using web pages", file=sys.stderr)
                self._login_web()
                self.token = None
                self.mode = "web-session"
        return self._list_files_web(course_id)

    def _list_files_api(self, course_id: int) -> list[FileItem]:
        sections = self.api_call("core_course_get_contents", courseid=course_id)
        files: list[FileItem] = []
        for section_position, section in enumerate(sections, 1):
            section_name = str(section.get("name") or f"Section {section.get('section', '')}")
            for activity_position, module in enumerate(section.get("modules", []), 1):
                activity = str(module.get("name") or module.get("modname") or "activity")
                for content in module.get("contents", []) or []:
                    file_url = content.get("fileurl")
                    if not file_url or content.get("type", "file") != "file":
                        continue
                    files.append(
                        FileItem(
                            course_id=course_id,
                            section=section_name,
                            activity=activity,
                            filename=str(content.get("filename") or "file"),
                            url=str(file_url),
                            mimetype=str(content.get("mimetype") or ""),
                            filesize=int(content["filesize"]) if content.get("filesize") is not None else None,
                            chapter=section_name,
                            section_id=int(section["id"]) if section.get("id") is not None else None,
                            section_number=int(section["section"]) if section.get("section") is not None else section_position,
                            activity_id=int(module["id"]) if module.get("id") is not None else None,
                            activity_type=str(module.get("modname") or ""),
                            activity_position=activity_position,
                            description=re.sub(r"<[^>]+>", " ", str(module.get("description") or "")).strip(),
                        )
                    )
        return _unique(files)

    def _list_files_web(self, course_id: int) -> list[FileItem]:
        page, final_url, _ = self._read_text(f"/course/view.php?id={course_id}")
        structure_parser = _CourseStructureParser(final_url)
        structure_parser.feed(page)
        structure = structure_parser.result()
        self._course_structure_cache[course_id] = structure
        activity_by_url: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for section in structure:
            for activity_metadata in section["activities"]:
                if activity_metadata["url"]:
                    key = urllib.parse.urlsplit(activity_metadata["url"])._replace(fragment="").geturl()
                    activity_by_url[key] = (section, activity_metadata)
        parser = _LinkParser(final_url)
        parser.feed(page)
        module_links: list[tuple[str, str]] = []
        direct_files: list[FileItem] = []
        for url, label in parser.links:
            parsed = urllib.parse.urlparse(url)
            module_match = re.search(r"/mod/([^/]+)/view\.php$", parsed.path)
            if module_match and module_match.group(1) in DOWNLOADABLE_MODULES:
                module_links.append((url, label or module_match.group(1)))
            elif "/pluginfile.php/" in parsed.path:
                direct_files.append(self._web_file_item(course_id, "Course files", label, url))
        for url in parser.media:
            if "/pluginfile.php/" in urllib.parse.urlparse(url).path:
                direct_files.append(self._web_file_item(course_id, "Course files", "media", url))

        files = list(direct_files)
        seen_modules: set[str] = set()
        for module_url, activity in module_links:
            module_key = urllib.parse.urlsplit(module_url)._replace(query=urllib.parse.urlencode(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(module_url).query))), fragment="").geturl()
            if module_key in seen_modules:
                continue
            seen_modules.add(module_key)
            metadata = activity_by_url.get(module_key)
            if metadata:
                activity = metadata[1]["name"] or activity
            separator = "&" if urllib.parse.urlparse(module_url).query else "?"
            inspect_url = module_url + separator + "redirect=0"
            try:
                module_page, module_final_url, headers = self._read_text(inspect_url)
            except MoodleError as exc:
                print(f"warning: skipped {activity}: {exc}", file=sys.stderr)
                continue
            content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
            if not (content_type.startswith("text/") or content_type in {"application/xhtml+xml", "application/xml"}):
                files.append(self._web_file_item(course_id, "Course files", activity, module_final_url, metadata))
                continue
            module_parser = _LinkParser(module_final_url)
            module_parser.feed(module_page)
            candidates = list(module_parser.media)
            candidates.extend(url for url, _ in module_parser.links)
            for candidate in candidates:
                candidate_path = urllib.parse.urlparse(candidate).path
                if "/pluginfile.php/" in candidate_path or "/webservice/pluginfile.php/" in candidate_path:
                    files.append(self._web_file_item(course_id, "Course files", activity, candidate, metadata))
        return _unique(files)

    def _web_file_item(
        self,
        course_id: int,
        section: str,
        activity: str,
        url: str,
        metadata: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> FileItem:
        path_name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        section_metadata, activity_metadata = metadata or ({}, {})
        return FileItem(
            course_id=course_id,
            section=section,
            activity=activity or path_name,
            filename=path_name or _clean_name(activity),
            url=url,
            chapter=str(section_metadata.get("name") or section),
            section_id=section_metadata.get("id"),
            section_number=section_metadata.get("number"),
            activity_id=activity_metadata.get("id"),
            activity_type=str(activity_metadata.get("type") or ""),
            activity_position=activity_metadata.get("position"),
            description=str(activity_metadata.get("description") or ""),
        )

    def course_structure(self, course_id: int) -> list[dict[str, Any]]:
        cached = self._course_structure_cache.get(course_id)
        if cached is not None:
            return cached
        page, final_url, _ = self._read_text(f"/course/view.php?id={course_id}")
        parser = _CourseStructureParser(final_url)
        parser.feed(page)
        structure = parser.result()
        self._course_structure_cache[course_id] = structure
        return structure

    def download(
        self,
        item: FileItem,
        root: Path,
        overwrite: bool = False,
        retries: int = 5,
        retry_delay: float = 1.0,
    ) -> Path:
        section = _clean_name(item.section, "Course files")
        activity = _clean_name(item.activity, "activity")
        directory = root / _clean_name(str(item.course_id), "course") / section
        directory.mkdir(parents=True, exist_ok=True)

        url = item.url
        if self.token:
            parts = urllib.parse.urlsplit(url)
            query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            query.setdefault("token", self.token)
            url = parts._replace(query=urllib.parse.urlencode(query)).geturl()

        suggested_filename = _clean_name(item.filename, activity)
        destination = directory / suggested_filename
        if destination.exists() and not overwrite:
            return destination
        temporary = destination.with_name(destination.name + ".part")
        if overwrite:
            temporary.unlink(missing_ok=True)

        expected_total = item.filesize
        started = time.monotonic()
        retry_number = 0
        while True:
            resume_at = temporary.stat().st_size if temporary.exists() else 0
            headers: dict[str, str] = {}
            if resume_at:
                headers["Range"] = f"bytes={resume_at}-"
            try:
                with self._request(url, headers=headers, timeout=120) as response:
                    status_code = getattr(response, "status", response.getcode())
                    response_filename = _filename_from_headers(response.headers, response.geturl(), activity)
                    if not temporary.exists() and response_filename != suggested_filename:
                        destination = directory / response_filename
                        if destination.exists() and not overwrite:
                            return destination
                        temporary = destination.with_name(destination.name + ".part")
                        resume_at = temporary.stat().st_size if temporary.exists() else 0

                    content_range = response.headers.get("Content-Range", "")
                    range_match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range.strip(), flags=re.I)
                    if resume_at and status_code == 206:
                        if not range_match or int(range_match.group(1)) != resume_at:
                            raise MoodleError("server returned an unexpected byte range")
                        if range_match.group(3) != "*":
                            expected_total = int(range_match.group(3))
                        file_mode = "ab"
                        downloaded = resume_at
                    elif resume_at and status_code == 200:
                        print(
                            f"warning: server did not honor resume for {destination.name}; restarting this file",
                            file=sys.stderr,
                        )
                        file_mode = "wb"
                        downloaded = 0
                        resume_at = 0
                    else:
                        file_mode = "wb"
                        downloaded = 0

                    content_length = response.headers.get("Content-Length", "")
                    if expected_total is None and content_length.isdigit():
                        expected_total = downloaded + int(content_length)

                    with temporary.open(file_mode) as output:
                        while True:
                            try:
                                chunk = response.read(1024 * 1024)
                            except http.client.IncompleteRead as exc:
                                if exc.partial:
                                    output.write(exc.partial)
                                    downloaded += len(exc.partial)
                                    output.flush()
                                raise
                            if not chunk:
                                break
                            output.write(chunk)
                            downloaded += len(chunk)
                            if sys.stderr.isatty():
                                if expected_total:
                                    progress = f"{downloaded / expected_total:6.1%}"
                                else:
                                    progress = f"{downloaded / 1024 / 1024:7.1f} MiB"
                                print(
                                    f"\r  {progress}  {destination.name[:60]}",
                                    end="",
                                    file=sys.stderr,
                                    flush=True,
                                )
                    if expected_total is not None and downloaded != expected_total:
                        raise MoodleError(
                            f"connection closed early at {downloaded} of {expected_total} bytes"
                        )
                temporary.replace(destination)
                if sys.stderr.isatty():
                    elapsed = max(time.monotonic() - started, 0.01)
                    rate = downloaded / 1024 / 1024 / elapsed
                    print(
                        f"\r  done {downloaded / 1024 / 1024:7.1f} MiB ({rate:.1f} MiB/s)  {destination.name}",
                        file=sys.stderr,
                    )
                return destination
            except (
                MoodleError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as exc:
                retry_number += 1
                if retry_number > retries:
                    saved = temporary.stat().st_size if temporary.exists() else 0
                    raise MoodleError(
                        f"download failed after {retries + 1} attempts; "
                        f"kept {saved / 1024 / 1024:.1f} MiB in {temporary} ({exc})"
                    ) from exc
                saved = temporary.stat().st_size if temporary.exists() else 0
                delay = min(retry_delay * (2 ** (retry_number - 1)), 15.0)
                print(
                    f"\n  connection interrupted; retry {retry_number}/{retries} "
                    f"from {saved / 1024 / 1024:.1f} MiB in {delay:g}s",
                    file=sys.stderr,
                )
                if delay:
                    time.sleep(delay)


def _env_value(args: argparse.Namespace, key: str) -> str:
    return os.environ.get(key) or args.env_values.get(key, "")


def _credentials(args: argparse.Namespace, *, fresh: bool = False) -> tuple[str, str]:
    username = "" if fresh else args.username or _env_value(args, ENV_KEYS["username"])
    password = "" if fresh else _env_value(args, ENV_KEYS["password"])
    if not username:
        username = input("LMS username: ").strip()
    if not password:
        password = getpass.getpass("LMS password: ")
    if not username or not password:
        raise MoodleError("username and password are required")
    return username, password


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"warning: could not read {path} ({exc})", file=sys.stderr)
        return {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, raw_value = stripped.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        raw_value = raw_value.strip()
        try:
            if raw_value.startswith('"') and raw_value.endswith('"'):
                value = json.loads(raw_value)
            elif raw_value.startswith("'") and raw_value.endswith("'"):
                value = raw_value[1:-1]
            else:
                value = raw_value
        except json.JSONDecodeError:
            value = raw_value.strip('"')
        values[key] = value
    return values


def _save_env_values(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError as exc:
        raise MoodleError(f"could not read {path}: {exc}") from exc
    rendered = {key: f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in updates.items()}
    output_lines: list[str] = []
    written: set[str] = set()
    assignment = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in existing_lines:
        match = assignment.match(line)
        key = match.group(1) if match else ""
        if key in rendered:
            if key not in written:
                output_lines.append(rendered[key])
                written.add(key)
            continue
        output_lines.append(line)
    for key, line in rendered.items():
        if key not in written:
            output_lines.append(line)
    payload = "\n".join(output_lines).rstrip() + "\n"
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _browser_session_values(args: argparse.Namespace, *, fresh: bool = False) -> tuple[str, str] | None:
    if fresh:
        return None
    user_agent = args.browser_user_agent or _env_value(args, ENV_KEYS["user_agent"])
    cookie_header = _env_value(args, ENV_KEYS["cookie"])
    if user_agent and cookie_header:
        return user_agent, cookie_header
    return None


def _prompt_browser_session(args: argparse.Namespace, *, fresh: bool = False) -> tuple[str, str]:
    stored = _browser_session_values(args, fresh=fresh)
    user_agent, cookie_header = stored or ("", "")
    if not user_agent:
        user_agent = input("Browser User-Agent header: ").strip()
    if not cookie_header:
        cookie_header = getpass.getpass("Browser Cookie header (hidden): ").strip()
    if not user_agent or not cookie_header:
        raise MoodleError("both the browser User-Agent and Cookie headers are required")
    return user_agent, cookie_header


def _find_browser_executable(explicit: Path | None = None) -> Path | None:
    if explicit:
        candidate = explicit.expanduser().resolve()
        return candidate if candidate.is_file() else None
    candidates = [
        Path("/Applications/Zen.app/Contents/MacOS/zen"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Firefox.app/Contents/MacOS/firefox"),
    ]
    for command in ("zen", "firefox", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _browser_login_url(args: argparse.Namespace) -> str:
    configured = getattr(args, "login_url", None) or _env_value(args, ENV_KEYS["login_url"])
    if configured:
        return configured
    hostname = (urllib.parse.urlsplit(args.site).hostname or "").casefold()
    if hostname == "training.ravinacademy.com":
        return DEFAULT_RAVIN_LOGIN_URL
    return urllib.parse.urljoin(args.site.rstrip("/") + "/", "my/courses.php")


def _capture_browser_session(args: argparse.Namespace) -> tuple[str, str]:
    """Open an installed browser through Selenium and capture its authenticated session."""
    try:
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise MoodleError(
            "automatic browser login is not installed. Run:\n"
            "  python3 -m pip install 'ravin-moodle-downloader[browser]'"
        ) from exc

    executable = _find_browser_executable(args.browser_executable)
    if executable is None:
        raise MoodleError(
            "no supported browser was found. Install Firefox/Zen/Chrome, or pass "
            "--browser-executable /path/to/browser"
        )

    profile = args.browser_profile.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(profile, 0o700)
    except OSError:
        pass

    browser_name = executable.name.casefold()
    try:
        if "firefox" in browser_name or "zen" in browser_name:
            from selenium.webdriver.firefox.options import Options
            from selenium.webdriver.firefox.service import Service

            options = Options()
            options.binary_location = str(executable)
            options.add_argument("-profile")
            options.add_argument(str(profile))
            driver_path = shutil.which("geckodriver")
            service = Service(executable_path=driver_path) if driver_path else Service()
            driver = webdriver.Firefox(options=options, service=service)
        else:
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.binary_location = str(executable)
            options.add_argument(f"--user-data-dir={profile}")
            options.add_argument("--no-first-run")
            driver = webdriver.Chrome(options=options)
    except WebDriverException as exc:
        raise MoodleError(
            f"could not launch {executable.name}: {exc}. "
            "Selenium Manager may need network access once to install the matching driver."
        ) from exc

    login_url = _browser_login_url(args)
    username = _env_value(args, ENV_KEYS["username"])
    password = _env_value(args, ENV_KEYS["password"])
    deadline = time.monotonic() + max(args.login_timeout, 30)
    print(f"Opening {executable.name} for LMS authentication...", file=sys.stderr)
    print("Complete Cloudflare or LMS login in that window if requested.", file=sys.stderr)
    try:
        driver.get(login_url)
        submitted_credentials = False
        credential_attempts = 0
        last_location = ""
        while time.monotonic() < deadline:
            current_location = urllib.parse.urlsplit(driver.current_url)._replace(query="", fragment="").geturl()
            if current_location != last_location:
                print(f"Browser is at {current_location}", file=sys.stderr)
                last_location = current_location
            try:
                logged_in = bool(
                    driver.execute_script(
                        """return Boolean(
                            window.M && M.cfg && Number(M.cfg.userId) > 0 &&
                            document.querySelector('a[href*="/login/logout.php"]')
                        );"""
                    )
                )
            except WebDriverException:
                logged_in = False
            if logged_in:
                cookies = driver.get_cookies()
                user_agent = str(driver.execute_script("return navigator.userAgent;"))
                cookie_header = "; ".join(
                    f"{cookie['name']}={cookie['value']}"
                    for cookie in cookies
                    if cookie.get("name") and cookie.get("value")
                )
                if not cookie_header:
                    raise MoodleError("browser login succeeded, but no site cookies were available")
                return user_agent, cookie_header

            # Ravin's account portal owns the login. Its Moodle launch link creates
            # the session on training.ravinacademy.com and redirects there.
            launch_links = driver.find_elements(
                By.CSS_SELECTOR,
                'a[href*="/moodle/login_student_user/"]',
            )
            launch_urls = [
                element.get_attribute("href")
                for element in launch_links
                if element.is_displayed() and element.get_attribute("href")
            ]
            if launch_urls:
                print("LMS login accepted; opening the Moodle course portal.", file=sys.stderr)
                driver.get(launch_urls[0])
                submitted_credentials = False
                continue

            if submitted_credentials:
                error_elements = driver.find_elements(
                    By.CSS_SELECTOR,
                    ".loginerrors, .alert-danger, .invalid-feedback, "
                    ".field-validation-error, .error-message, .toast-error, "
                    ".swal2-validation-message, [data-region=\"login-error\"], [role=\"alert\"]",
                )
                error_messages = [
                    " ".join(element.text.split())
                    for element in error_elements
                    if element.is_displayed() and element.text.strip()
                ]
                if error_messages:
                    if credential_attempts >= 3:
                        raise MoodleError(f"the LMS rejected the login: {error_messages[0][:300]}")
                    print(f"The LMS rejected the saved login: {error_messages[0][:300]}", file=sys.stderr)
                    print("Enter fresh credentials in this terminal.", file=sys.stderr)
                    username, password = _credentials(args, fresh=True)
                    _save_env_values(
                        args.env_file,
                        {
                            ENV_KEYS["username"]: username,
                            ENV_KEYS["password"]: password,
                        },
                    )
                    args.env_values.update(
                        {
                            ENV_KEYS["username"]: username,
                            ENV_KEYS["password"]: password,
                        }
                    )
                    submitted_credentials = False

            if username and password and not submitted_credentials:
                login_controls = driver.execute_script(
                    """const visible = (element) => {
                        const style = window.getComputedStyle(element);
                        const box = element.getBoundingClientRect();
                        return !element.disabled && style.display !== 'none' &&
                            style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
                    };
                    const passwords = [...document.querySelectorAll('input[type="password"]')]
                        .filter(visible);
                    if (passwords.length !== 1) return null;
                    const password = passwords[0];
                    const form = password.form || password.closest('form');
                    if (!form) return null;
                    const candidates = [...form.querySelectorAll('input')].filter((element) => {
                        const type = (element.type || 'text').toLowerCase();
                        return visible(element) && ![
                            'password', 'hidden', 'submit', 'button', 'checkbox',
                            'radio', 'file', 'reset'
                        ].includes(type);
                    });
                    const preferred = /user|mobile|phone|email|login|national/i;
                    const username = candidates.find((element) => preferred.test(
                        `${element.name} ${element.id} ${element.autocomplete}`
                    )) || candidates[0];
                    const submits = [...form.querySelectorAll('button, input[type="submit"]')]
                        .filter((element) => visible(element) && element.type === 'submit');
                    return username && submits.length ? [username, password, submits[0]] : null;"""
                )
                if login_controls:
                    username_input, password_input, submit_button = login_controls
                    driver.execute_script(
                        """const setValue = (element, value) => {
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(element, value);
                            element.dispatchEvent(new Event('input', {bubbles: true}));
                            element.dispatchEvent(new Event('change', {bubbles: true}));
                        };
                        setValue(arguments[0], arguments[1]);
                        setValue(arguments[2], arguments[3]);
                        const form = arguments[0].closest('form');
                        if (form && form.requestSubmit) {
                            form.requestSubmit(arguments[4]);
                        } else {
                            arguments[4].click();
                        }""",
                        username_input,
                        username,
                        password_input,
                        password,
                        submit_button,
                    )
                    submitted_credentials = True
                    credential_attempts += 1
                    print("Submitted the stored LMS credentials.", file=sys.stderr)
            time.sleep(1)
        raise MoodleError(f"browser login did not finish within {max(args.login_timeout, 30)} seconds")
    except WebDriverException as exc:
        raise MoodleError(f"browser authentication failed: {exc}") from exc
    finally:
        driver.quit()


def _authenticate_browser_session(
    args: argparse.Namespace,
    saved: tuple[str, str] | None = None,
) -> tuple[MoodleClient, str]:
    from_saved = saved is not None and not args.refresh_session
    if from_saved:
        user_agent, cookie_header = saved
    elif args.manual_session:
        user_agent, cookie_header = _prompt_browser_session(args, fresh=True)
    else:
        user_agent, cookie_header = _capture_browser_session(args)
    client = MoodleClient(
        args.site,
        cookie_header=cookie_header,
        browser_user_agent=user_agent,
    )
    try:
        mode = client.authenticate()
    except MoodleError as exc:
        if not from_saved:
            raise
        print(f"Saved browser session is no longer valid ({exc}).", file=sys.stderr)
        if args.manual_session:
            print("Please paste fresh headers from a logged-in browser request.", file=sys.stderr)
            user_agent, cookie_header = _prompt_browser_session(args, fresh=True)
        else:
            print("Opening the authentication browser to refresh it.", file=sys.stderr)
            user_agent, cookie_header = _capture_browser_session(args)
        client = MoodleClient(
            args.site,
            cookie_header=cookie_header,
            browser_user_agent=user_agent,
        )
        mode = client.authenticate()
    _save_env_values(
        args.env_file,
        {
            ENV_KEYS["login_url"]: _browser_login_url(args),
            ENV_KEYS["user_agent"]: user_agent,
            ENV_KEYS["cookie"]: cookie_header,
        },
    )
    args.env_values.update(
        {
            ENV_KEYS["login_url"]: _browser_login_url(args),
            ENV_KEYS["user_agent"]: user_agent,
            ENV_KEYS["cookie"]: cookie_header,
        }
    )
    return client, mode


def _format_size(size: int | None) -> str:
    if size is None:
        return "?"
    units = ["B", "KiB", "MiB", "GiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def _resource_kind(item: FileItem) -> str:
    mimetype = item.mimetype.casefold()
    suffix = Path(item.filename).suffix.casefold()
    if mimetype.startswith("video/") or suffix in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}:
        return "video"
    if mimetype.startswith("audio/") or suffix in {".mp3", ".m4a", ".wav", ".ogg", ".flac"}:
        return "audio"
    if (
        mimetype.startswith("text/")
        or mimetype in {"application/pdf", "application/msword"}
        or suffix in {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".md"}
    ):
        return "document"
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        return "archive"
    return "file"


def _media_browser_path(path: Path, downloads: Path) -> str:
    try:
        relative = path.resolve().relative_to(downloads.resolve())
    except ValueError as exc:
        raise MoodleError(f"downloaded file is outside the configured download root: {path}") from exc
    return "media/" + "/".join(urllib.parse.quote(part) for part in relative.parts)


def _local_resource(item: FileItem, downloads: Path) -> tuple[str, Path | None]:
    directory = downloads / _clean_name(str(item.course_id), "course") / _clean_name(
        item.section,
        "Course files",
    )
    expected = directory / _clean_name(item.filename, _clean_name(item.activity, "activity"))
    if expected.is_file():
        return "downloaded", expected
    partial = expected.with_name(expected.name + ".part")
    if partial.is_file():
        return "partial", partial
    if directory.is_dir():
        expected_name = expected.name.casefold()
        matches = [path for path in directory.iterdir() if path.is_file() and path.name.casefold() == expected_name]
        if len(matches) == 1:
            return "downloaded", matches[0]
    return "missing", None


def _build_library_catalog(
    client: MoodleClient,
    downloads: Path,
    output: Path,
    selected_course_ids: Iterable[int] = (),
) -> dict[str, Any]:
    selected_ids = set(selected_course_ids)
    available_courses = client.list_courses()
    courses = [course for course in available_courses if not selected_ids or course.id in selected_ids]
    missing_ids = selected_ids - {course.id for course in courses}
    if missing_ids:
        missing = ", ".join(str(course_id) for course_id in sorted(missing_ids))
        raise MoodleError(f"course ID(s) not found in your enrollments: {missing}")

    catalog_courses: list[dict[str, Any]] = []
    total_files = 0
    total_activities = 0
    total_records = 0
    total_downloaded = 0
    total_downloaded_bytes = 0
    for course in courses:
        print(f"Reading course {course.id}: {course.fullname}", file=sys.stderr)
        downloaded_count = 0
        downloaded_bytes = 0
        items = client.list_files(course.id)
        try:
            structure_method = getattr(client, "course_structure")
            structure = structure_method(course.id)
        except (AttributeError, MoodleError):
            structure = []
        consumed: set[int] = set()

        def file_record(
            item: FileItem,
            item_index: int,
            section_metadata: dict[str, Any] | None = None,
            activity_metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            nonlocal downloaded_count, downloaded_bytes
            section_metadata = section_metadata or {}
            activity_metadata = activity_metadata or {}
            status, local_path = _local_resource(item, downloads)
            local_bytes = local_path.stat().st_size if local_path else 0
            if status == "downloaded":
                downloaded_count += 1
                downloaded_bytes += local_bytes
            stable_source = urllib.parse.urlsplit(item.url)._replace(query="", fragment="").geturl()
            resource_id = hashlib.sha1(
                f"{course.id}\0{stable_source}\0{item.filename}".encode("utf-8")
            ).hexdigest()[:14]
            title = activity_metadata.get("name") or re.sub(r"\s*فایل\s*$", "", item.activity).strip()
            consumed.add(item_index)
            return {
                "id": resource_id,
                "section": item.section,
                "section_id": section_metadata.get("id", item.section_id),
                "section_number": section_metadata.get("number", item.section_number),
                "activity_id": activity_metadata.get("id", item.activity_id),
                "activity_position": activity_metadata.get("position", item.activity_position),
                "activity_type": activity_metadata.get("type", item.activity_type or "resource"),
                "title": title or item.filename,
                "description": activity_metadata.get("description") or item.description,
                "badge": activity_metadata.get("badge") or Path(item.filename).suffix.removeprefix(".").upper(),
                "filename": item.filename,
                "extension": Path(item.filename).suffix.removeprefix(".").casefold(),
                "kind": _resource_kind(item),
                "mimetype": item.mimetype,
                "status": status,
                "size": local_bytes if status == "downloaded" else item.filesize,
                "local_bytes": local_bytes,
                "local_url": _media_browser_path(local_path, downloads) if status == "downloaded" and local_path else None,
                "source_url": activity_metadata.get("url") or f"{client.site}/course/view.php?id={course.id}",
                "lms_completed": activity_metadata.get("lms_completed"),
            }

        files_by_activity: dict[int, list[tuple[int, FileItem]]] = {}
        for item_index, item in enumerate(items):
            if item.activity_id is not None:
                files_by_activity.setdefault(item.activity_id, []).append((item_index, item))

        catalog_sections: list[dict[str, Any]] = []
        activity_count = 0
        for section in structure:
            section_records: list[dict[str, Any]] = []
            activities = section.get("activities", [])
            activity_count += len(activities)
            for activity in activities:
                matches = files_by_activity.get(activity.get("id"), [])
                if not matches:
                    activity_name = re.sub(r"\s*فایل\s*$", "", str(activity.get("name") or "")).strip().casefold()
                    matches = [
                        (item_index, item)
                        for item_index, item in enumerate(items)
                        if item_index not in consumed
                        and re.sub(r"\s*فایل\s*$", "", item.activity).strip().casefold() == activity_name
                    ]
                if matches:
                    for item_index, item in matches:
                        if item_index not in consumed:
                            section_records.append(file_record(item, item_index, section, activity))
                    continue
                activity_type = str(activity.get("type") or "activity")
                type_names = {
                    "bigbluebuttonbn": "live class",
                    "forum": "discussion",
                    "quiz": "quiz",
                    "url": "link",
                    "resource": "resource",
                }
                section_records.append(
                    {
                        "id": f"activity-{activity.get('id') or hashlib.sha1(str(activity).encode()).hexdigest()[:10]}",
                        "section": section.get("name") or "Course",
                        "section_id": section.get("id"),
                        "section_number": section.get("number"),
                        "activity_id": activity.get("id"),
                        "activity_position": activity.get("position"),
                        "activity_type": activity_type,
                        "title": activity.get("name") or activity_type,
                        "description": activity.get("description") or "",
                        "badge": activity.get("badge") or type_names.get(activity_type, activity_type),
                        "filename": "",
                        "extension": "",
                        "kind": type_names.get(activity_type, "activity"),
                        "mimetype": "",
                        "status": "online",
                        "size": None,
                        "local_bytes": 0,
                        "local_url": None,
                        "source_url": activity.get("url") or f"{client.site}/course/view.php?id={course.id}",
                        "lms_completed": activity.get("lms_completed"),
                    }
                )
            catalog_sections.append(
                {
                    "id": section.get("id"),
                    "number": section.get("number"),
                    "position": section.get("position"),
                    "name": section.get("name") or "Section",
                    "summary": section.get("summary") or "",
                    "activity_count": len(activities),
                    "items": section_records,
                }
            )

        for item_index, item in enumerate(items):
            if item_index in consumed:
                continue
            chapter_name = item.chapter or item.section
            target = next((section for section in catalog_sections if section["name"] == chapter_name), None)
            if target is None:
                target = {
                    "id": item.section_id,
                    "number": item.section_number,
                    "position": len(catalog_sections) + 1,
                    "name": chapter_name,
                    "summary": "",
                    "activity_count": 0,
                    "items": [],
                }
                catalog_sections.append(target)
            target["items"].append(file_record(item, item_index))

        if not structure:
            activity_count = len(items)
            for section in catalog_sections:
                section["activity_count"] = len(section["items"])
        file_count = len(items)
        record_count = sum(len(section["items"]) for section in catalog_sections)
        type_counts: dict[str, int] = {}
        for section in catalog_sections:
            for record in section["items"]:
                type_counts[record["kind"]] = type_counts.get(record["kind"], 0) + 1
        catalog_courses.append(
            {
                "id": course.id,
                "fullname": course.fullname,
                "shortname": course.shortname,
                "source_url": f"{client.site}/course/view.php?id={course.id}",
                "section_count": len(catalog_sections),
                "activity_count": activity_count,
                "record_count": record_count,
                "file_count": file_count,
                "downloaded_count": downloaded_count,
                "downloaded_bytes": downloaded_bytes,
                "type_counts": type_counts,
                "sections": catalog_sections,
            }
        )
        total_files += file_count
        total_activities += activity_count
        total_records += record_count
        total_downloaded += downloaded_count
        total_downloaded_bytes += downloaded_bytes

    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "courses": len(catalog_courses),
            "files": total_files,
            "activities": total_activities,
            "records": total_records,
            "downloaded_files": total_downloaded,
            "downloaded_bytes": total_downloaded_bytes,
        },
        "courses": catalog_courses,
    }


def _write_library_site(output: Path, catalog: dict[str, Any], downloads: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    asset_root = resources.files("ravin_downloader_assets")
    for name in LIBRARY_ASSETS:
        (output / name).write_bytes(asset_root.joinpath(name).read_bytes())
    catalog_path = output / "courses.json"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    media_link = output / "media"
    relative_target = Path(os.path.relpath(downloads.resolve(), output.resolve()))
    if os.path.lexists(media_link):
        if not media_link.is_symlink() or media_link.resolve() != downloads.resolve():
            raise MoodleError(
                f"{media_link} already exists and is not the expected download symlink; "
                "move it aside and rebuild the library"
            )
    else:
        try:
            media_link.symlink_to(relative_target, target_is_directory=True)
        except OSError as exc:
            raise MoodleError(f"could not create media symlink {media_link}: {exc}") from exc

    quoted_root = json.dumps(str(output.resolve()))
    (output / "nginx-server.conf").write_text(
        f"""# Generated by ravin-downloader. Include this file inside nginx's http block.
server {{
    listen 127.0.0.1:8765;
    server_name localhost;
    root {quoted_root};
    index index.html;
    charset utf-8;
    autoindex off;

    location = / {{
        try_files /index.html =404;
    }}

    location = /courses.json {{
        try_files $uri =404;
        default_type application/json;
        add_header Cache-Control "no-store" always;
    }}

    location /media/ {{
        try_files $uri =404;
        sendfile on;
        tcp_nopush on;
        add_header Accept-Ranges bytes always;
        add_header X-Content-Type-Options nosniff always;
    }}

    location / {{
        try_files $uri $uri/ =404;
    }}

    location ~ (^|/)\\. {{ return 404; }}
    location ~ \\.part$ {{ return 404; }}
}}
""",
        encoding="utf-8",
    )
    return catalog_path


def _reuse_library_catalog(output: Path, downloads: Path) -> dict[str, Any]:
    catalog_path = output / "courses.json"
    if not catalog_path.is_file():
        raise MoodleError(f"{catalog_path} does not exist; build the library online first")
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MoodleError(f"could not read the existing course catalog: {exc}") from exc
    total_files = 0
    total_downloaded = 0
    total_downloaded_bytes = 0
    for course in catalog.get("courses", []):
        course_id = int(course.get("id") or 0)
        course_root = downloads / _clean_name(str(course_id), "course")
        mapped_files: dict[str, str] = {}
        files_map = course_root / "files-map.txt"
        if files_map.is_file():
            for line in files_map.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.strip().split(" / ")
                if len(parts) < 3:
                    continue
                activity_name = re.sub(r"\s*فایل\s*$", "", html.unescape(parts[-2])).strip().casefold()
                mapped_files[activity_name] = parts[-1].strip()

        course_file_count = 0
        course_downloaded = 0
        course_downloaded_bytes = 0
        for section in course.get("sections", []):
            for item in section.get("items", []):
                item["title"] = html.unescape(str(item.get("title") or ""))
                item["description"] = html.unescape(str(item.get("description") or ""))
                local_url = item.get("local_url")
                if local_url and not str(local_url).startswith("media/"):
                    previous_path = (output / urllib.parse.unquote(str(local_url))).resolve()
                    item["local_url"] = _media_browser_path(previous_path, downloads)

                filename = str(item.get("filename") or "")
                if not filename and item.get("activity_type") == "resource":
                    activity_key = re.sub(
                        r"\s*فایل\s*$",
                        "",
                        html.unescape(str(item.get("title") or "")),
                    ).strip().casefold()
                    filename = mapped_files.get(activity_key, "")
                    if filename:
                        item["filename"] = filename
                        item["extension"] = Path(filename).suffix.removeprefix(".").casefold()
                        guessed_type = mimetypes.guess_type(filename)[0] or ""
                        item["mimetype"] = guessed_type
                        synthetic = FileItem(
                            course_id=course_id,
                            section="Course files",
                            activity=str(item.get("title") or filename),
                            filename=filename,
                            url="",
                            mimetype=guessed_type,
                        )
                        item["kind"] = _resource_kind(synthetic)

                if not filename:
                    continue
                course_file_count += 1
                matching = [
                    path
                    for path in course_root.rglob(filename)
                    if path.is_file() and not path.name.endswith(".part")
                ] if course_root.is_dir() else []
                if len(matching) == 1:
                    local_path = matching[0]
                    local_bytes = local_path.stat().st_size
                    item.update(
                        {
                            "status": "downloaded",
                            "size": local_bytes,
                            "local_bytes": local_bytes,
                            "local_url": _media_browser_path(local_path, downloads),
                        }
                    )
                    course_downloaded += 1
                    course_downloaded_bytes += local_bytes
                elif item.get("status") == "downloaded":
                    item.update({"status": "missing", "local_bytes": 0, "local_url": None})
        course["file_count"] = course_file_count
        course["downloaded_count"] = course_downloaded
        course["downloaded_bytes"] = course_downloaded_bytes
        total_files += course_file_count
        total_downloaded += course_downloaded
        total_downloaded_bytes += course_downloaded_bytes
    stats = catalog.setdefault("stats", {})
    stats.update(
        {
            "files": total_files,
            "downloaded_files": total_downloaded,
            "downloaded_bytes": total_downloaded_bytes,
        }
    )
    catalog["prepared_at"] = datetime.now(timezone.utc).isoformat()
    return catalog


class _RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static-file handler with byte ranges for efficient local media playback."""

    _byte_range: tuple[int, int] | None = None

    def send_head(self) -> Any:
        self._byte_range = None
        request_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        request_parts = [part for part in request_path.split("/") if part]
        if any(part.startswith(".") for part in request_parts) or request_path.casefold().endswith(".part"):
            self.send_error(404, "File not found")
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            source = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            size = os.fstat(source.fileno()).st_size
            range_header = self.headers.get("Range", "")
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:
                    suffix_length = int(match.group(2))
                    start = max(size - suffix_length, 0)
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    source.close()
                    return None
                end = min(end, size - 1)
                self._byte_range = (start, end)
                self.send_response(206)
                self.send_header("Content-Type", self.guess_type(path))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Last-Modified", self.date_time_string(os.fstat(source.fileno()).st_mtime))
                self.end_headers()
                return source
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(os.fstat(source.fileno()).st_mtime))
            self.end_headers()
            return source
        except Exception:
            source.close()
            raise

    def copyfile(self, source: Any, outputfile: Any) -> None:
        if self._byte_range is None:
            shutil.copyfileobj(source, outputfile)
            return
        start, end = self._byte_range
        source.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def _serve_library(site_dir: Path, downloads: Path, host: str, port: int, open_browser: bool) -> None:
    site_dir = site_dir.expanduser().resolve()
    downloads = downloads.expanduser().resolve()
    if not (site_dir / "index.html").is_file() or not (site_dir / "courses.json").is_file():
        raise MoodleError(f"{site_dir} is not built yet; run `ravin-downloader library` first")
    media_link = site_dir / "media"
    if not media_link.is_symlink() or media_link.resolve() != downloads:
        raise MoodleError(f"the media symlink is missing or incorrect; run `ravin-downloader library` again")
    handler = functools.partial(_RangeRequestHandler, directory=str(site_dir))
    try:
        server = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        raise MoodleError(f"could not start the library server on {host}:{port}: {exc}") from exc
    display_host = "localhost" if host in {"127.0.0.1", "0.0.0.0", "::"} else host
    url = f"http://{display_host}:{server.server_port}/"
    print(f"Learning Library: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List and download your Ravin Academy Moodle course files.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--site", default=DEFAULT_SITE, help=f"Moodle base URL (default: {DEFAULT_SITE})")
    parser.add_argument(
        "--login-url",
        help=(
            "account portal used for browser login; defaults to "
            f"{DEFAULT_RAVIN_LOGIN_URL} for Ravin Academy"
        ),
    )
    parser.add_argument("--username", help="LMS username; password is prompted securely")
    parser.add_argument("--web-only", action="store_true", help="skip the Moodle mobile API and use a normal web session")
    parser.add_argument(
        "--browser-session",
        action="store_true",
        help="force saved or automatic browser-session authentication",
    )
    parser.add_argument(
        "--browser-user-agent",
        help="User-Agent copied from the logged-in browser; otherwise prompted",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"credentials and browser-session file (default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--refresh-session",
        action="store_true",
        help="ignore the saved browser session and open the authentication browser",
    )
    parser.add_argument(
        "--manual-session",
        action="store_true",
        help="prompt for User-Agent and Cookie headers instead of opening a browser",
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=Path.cwd() / ".ravin-browser-profile",
        help="persistent profile for automatic browser login",
    )
    parser.add_argument(
        "--browser-executable",
        type=Path,
        help="installed Zen, Firefox, Chrome, Chromium, or Brave executable",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=600,
        help="seconds to wait for interactive browser login (default: 600)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="open a browser and refresh the saved LMS session")
    subparsers.add_parser("courses", help="list enrolled courses")
    files_parser = subparsers.add_parser("files", help="list downloadable files in a course")
    files_parser.add_argument("course_id", type=int)

    download_parser = subparsers.add_parser("download", help="download files from one course")
    download_parser.add_argument("course_id", type=int)
    download_parser.add_argument("--output", type=Path, default=Path("downloads"))
    download_parser.add_argument("--overwrite", action="store_true")
    download_parser.add_argument("--retries", type=int, default=5, help="retry interrupted files (default: 5)")
    library_parser = subparsers.add_parser(
        "library",
        help="generate a private static course library and courses.json",
    )
    library_parser.add_argument("course_ids", nargs="*", type=int, help="optional course IDs; defaults to all courses")
    library_parser.add_argument("--output", type=Path, default=Path("library"), help="generated site directory")
    library_parser.add_argument("--downloads", type=Path, default=Path("downloads"), help="download root to index")
    library_parser.add_argument(
        "--reuse-catalog",
        action="store_true",
        help="prepare pages, media symlink, and Nginx config without contacting the LMS",
    )
    serve_parser = subparsers.add_parser("serve-library", help="serve the generated library locally")
    serve_parser.add_argument("--site-dir", type=Path, default=Path("library"), help="generated site directory")
    serve_parser.add_argument("--downloads", type=Path, default=Path("downloads"), help="download root")
    serve_parser.add_argument("--host", default="127.0.0.1", help="local bind address (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    serve_parser.add_argument("--open", action="store_true", help="open the library in your default browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve-library":
            _serve_library(args.site_dir, args.downloads, args.host, args.port, args.open)
            return 0
        if args.command == "library" and args.reuse_catalog:
            output = args.output.expanduser().resolve()
            downloads = args.downloads.expanduser().resolve()
            catalog = _reuse_library_catalog(output, downloads)
            catalog_path = _write_library_site(output, catalog, downloads)
            print(f"Prepared {catalog_path}, media symlink, and Nginx configuration from existing data.")
            return 0
        args.env_values = _load_env_file(args.env_file)
        if args.command == "login":
            client, mode = _authenticate_browser_session(args)
            print(f"Authenticated using {mode}; updated {args.env_file}.")
            return 0
        saved_session = _browser_session_values(args, fresh=args.refresh_session)
        if args.browser_session or args.refresh_session or saved_session is not None:
            client, mode = _authenticate_browser_session(args, saved_session)
        else:
            username, password = _credentials(args)
            _save_env_values(
                args.env_file,
                {
                    ENV_KEYS["username"]: username,
                    ENV_KEYS["password"]: password,
                },
            )
            args.env_values.update(
                {
                    ENV_KEYS["username"]: username,
                    ENV_KEYS["password"]: password,
                }
            )
            client = MoodleClient(args.site, username, password, web_only=args.web_only)
            try:
                mode = client.authenticate()
            except MoodleError as exc:
                if "cloudflare requires a real browser session" not in str(exc).casefold():
                    raise
                print("Cloudflare blocked password login; opening the authentication browser.", file=sys.stderr)
                client, mode = _authenticate_browser_session(args)
        print(f"Authenticated using {mode}.", file=sys.stderr)

        if args.command == "courses":
            courses = client.list_courses()
            if args.json:
                print(json.dumps([asdict(course) for course in courses], ensure_ascii=False, indent=2))
            else:
                for course in courses:
                    suffix = f" [{course.shortname}]" if course.shortname else ""
                    print(f"{course.id:>6}  {course.fullname}{suffix}")
            return 0

        if args.command == "library":
            output = args.output.expanduser().resolve()
            downloads = args.downloads.expanduser().resolve()
            catalog = _build_library_catalog(client, downloads, output, args.course_ids)
            catalog_path = _write_library_site(output, catalog, downloads)
            print(f"Generated {catalog_path} and the static library pages.")
            print("View it with: ravin-downloader serve-library --open")
            return 0

        files = client.list_files(args.course_id)
        if args.command == "files":
            if args.json:
                print(json.dumps([asdict(item) for item in files], ensure_ascii=False, indent=2))
            else:
                for number, item in enumerate(files, 1):
                    print(f"{number:>3}. {_format_size(item.filesize):>10}  {item.section} / {item.activity} / {item.filename}")
            return 0

        if not files:
            print("No downloadable files were found.")
            return 0
        downloaded: list[str] = []
        for number, item in enumerate(files, 1):
            print(f"[{number}/{len(files)}] {item.activity}: {item.filename}", file=sys.stderr)
            path = client.download(
                item,
                args.output,
                overwrite=args.overwrite,
                retries=max(args.retries, 0),
            )
            downloaded.append(str(path))
        if args.json:
            print(json.dumps(downloaded, ensure_ascii=False, indent=2))
        else:
            print(f"Downloaded {len(downloaded)} file(s) to {args.output.resolve()}")
        return 0
    except (MoodleError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
