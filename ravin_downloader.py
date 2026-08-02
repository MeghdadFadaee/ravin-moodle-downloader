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
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SITE = "https://training.ravinacademy.com"
USER_AGENT = "RavinCourseDownloader/1.0 (+personal Moodle client)"
DOWNLOADABLE_MODULES = {"resource", "folder", "page", "book"}


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

    def download(self, item: FileItem, root: Path, overwrite: bool = False) -> Path:
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

        with self._request(url, timeout=120) as response:
            filename = _filename_from_headers(response.headers, response.geturl(), activity)
            destination = directory / filename
            if destination.exists() and not overwrite:
                return destination
            temporary = destination.with_name(destination.name + ".part")
            total = response.headers.get("Content-Length")
            expected = int(total) if total and total.isdigit() else None
            downloaded = 0
            started = time.monotonic()
            with temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if sys.stderr.isatty():
                        if expected:
                            status = f"{downloaded / expected:6.1%}"
                        else:
                            status = f"{downloaded / 1024 / 1024:7.1f} MiB"
                        print(f"\r  {status}  {filename[:60]}", end="", file=sys.stderr, flush=True)
            if expected is not None and downloaded != expected:
                temporary.unlink(missing_ok=True)
                raise MoodleError(f"incomplete download for {filename}: got {downloaded} of {expected} bytes")
            temporary.replace(destination)
            if sys.stderr.isatty():
                elapsed = max(time.monotonic() - started, 0.01)
                rate = downloaded / 1024 / 1024 / elapsed
                print(f"\r  done {downloaded / 1024 / 1024:7.1f} MiB ({rate:.1f} MiB/s)  {filename}", file=sys.stderr)
            return destination


def _credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = args.username or os.environ.get("RAVIN_USERNAME")
    password = os.environ.get("RAVIN_PASSWORD")
    if not username:
        username = input("LMS username: ").strip()
    if not password:
        password = getpass.getpass("LMS password: ")
    if not username or not password:
        raise MoodleError("username and password are required")
    return username, password


def _browser_session(args: argparse.Namespace) -> tuple[str, str]:
    user_agent = args.browser_user_agent or os.environ.get("RAVIN_USER_AGENT", "")
    cookie_header = os.environ.get("RAVIN_COOKIE", "")
    if not user_agent:
        user_agent = input("Browser User-Agent header: ").strip()
    if not cookie_header:
        cookie_header = getpass.getpass("Browser Cookie header (hidden): ").strip()
    if not user_agent or not cookie_header:
        raise MoodleError("both the browser User-Agent and Cookie headers are required")
    return user_agent, cookie_header


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
    parser.add_argument("--site", default=DEFAULT_SITE, help=f"Moodle base URL (default: {DEFAULT_SITE})")
    parser.add_argument("--username", help="LMS username; password is prompted securely")
    parser.add_argument("--web-only", action="store_true", help="skip the Moodle mobile API and use a normal web session")
    parser.add_argument(
        "--browser-session",
        action="store_true",
        help="reuse request headers from an already logged-in browser (works with Cloudflare)",
    )
    parser.add_argument(
        "--browser-user-agent",
        help="User-Agent copied from the logged-in browser; otherwise prompted",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("courses", help="list enrolled courses")
    files_parser = subparsers.add_parser("files", help="list downloadable files in a course")
    files_parser.add_argument("course_id", type=int)

    download_parser = subparsers.add_parser("download", help="download files from one course")
    download_parser.add_argument("course_id", type=int)
    download_parser.add_argument("--output", type=Path, default=Path("downloads"))
    download_parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.browser_session:
            browser_user_agent, cookie_header = _browser_session(args)
            client = MoodleClient(
                args.site,
                cookie_header=cookie_header,
                browser_user_agent=browser_user_agent,
            )
        else:
            username, password = _credentials(args)
            client = MoodleClient(args.site, username, password, web_only=args.web_only)
        mode = client.authenticate()
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
            path = client.download(item, args.output, overwrite=args.overwrite)
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
