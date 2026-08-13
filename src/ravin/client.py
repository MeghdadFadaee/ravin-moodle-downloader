"""Authenticated Moodle API/web client and resumable downloader."""

import http.client
import http.cookiejar
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .constants import DOWNLOADABLE_MODULES, USER_AGENT
from .local_files import archive_existing_file
from .models import Course, FileItem, MoodleError
from .parsers import _CourseStructureParser, _FormParser, _LinkParser, _parse_moodle_config
from .paths import (
    _activity_root,
    _clean_name,
    _filename_from_headers,
    _is_cloudflare_challenge,
    _unique,
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
        library: Path,
        overwrite: bool = False,
        retries: int = 5,
        retry_delay: float = 1.0,
    ) -> Path:
        activity = _clean_name(item.activity, "activity")
        activity_directory = _activity_root(library, item)
        directory = activity_directory / "files"
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
            if item.filesize is None or destination.stat().st_size == item.filesize:
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
                            if expected_total is None or destination.stat().st_size == expected_total:
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
                if destination.exists():
                    archived = archive_existing_file(destination)
                    print(f"  archived previous version as {archived.name}", file=sys.stderr)
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
