#!/usr/bin/env python3
"""List and download files from an enrolled Moodle course.

The client prefers Moodle's documented mobile web-service API.  If that
service is disabled by the site administrator, it falls back to an ordinary
web login plus the same AJAX endpoint used by Moodle's "My courses" page.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import getpass
import html
import http.cookiejar
import http.client
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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SITE = "https://training.ravinacademy.com"
DEFAULT_RAVIN_LOGIN_URL = "https://lms.ravinacademy.com/"
__version__ = "0.2.0"
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
        for section in sections:
            section_name = str(section.get("name") or f"Section {section.get('section', '')}")
            for module in section.get("modules", []):
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
                        )
                    )
        return _unique(files)

    def _list_files_web(self, course_id: int) -> list[FileItem]:
        page, final_url, _ = self._read_text(f"/course/view.php?id={course_id}")
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
            separator = "&" if urllib.parse.urlparse(module_url).query else "?"
            inspect_url = module_url + separator + "redirect=0"
            try:
                module_page, module_final_url, headers = self._read_text(inspect_url)
            except MoodleError as exc:
                print(f"warning: skipped {activity}: {exc}", file=sys.stderr)
                continue
            content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
            if not (content_type.startswith("text/") or content_type in {"application/xhtml+xml", "application/xml"}):
                files.append(self._web_file_item(course_id, "Course files", activity, module_final_url))
                continue
            module_parser = _LinkParser(module_final_url)
            module_parser.feed(module_page)
            candidates = list(module_parser.media)
            candidates.extend(url for url, _ in module_parser.links)
            for candidate in candidates:
                candidate_path = urllib.parse.urlparse(candidate).path
                if "/pluginfile.php/" in candidate_path or "/webservice/pluginfile.php/" in candidate_path:
                    files.append(self._web_file_item(course_id, "Course files", activity, candidate))
        return _unique(files)

    def _web_file_item(self, course_id: int, section: str, activity: str, url: str) -> FileItem:
        path_name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        return FileItem(
            course_id=course_id,
            section=section,
            activity=activity or path_name,
            filename=path_name or _clean_name(activity),
            url=url,
        )

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
