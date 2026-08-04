import email.message
import http.client
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin import Course, FileItem, MoodleClient
from ravin.auth import (
    _browser_login_url,
    _capture_browser_session,
    _load_env_file,
    _save_env_values,
)
from ravin.cli import build_parser
from ravin.library import (
    _build_library_catalog,
    _migrate_legacy_downloads,
    _reuse_library_catalog,
    _write_library_site,
)
from ravin.parsers import (
    _CourseStructureParser,
    _FormParser,
    _LinkParser,
    _parse_moodle_config,
)
from ravin.paths import (
    _activity_directory_name,
    _clean_name,
    _filename_from_headers,
    _is_cloudflare_challenge,
    _unique,
)


class _FakeResponse:
    def __init__(self, chunks, *, status=200, content_range=""):
        self.chunks = list(chunks)
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Disposition"] = 'attachment; filename="file.bin"'
        if content_range:
            self.headers["Content-Range"] = content_range

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def geturl(self):
        return "https://training.example/pluginfile.php/file.bin"

    def read(self, _size):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _FakeDownloadClient(MoodleClient):
    def __init__(self, responses):
        super().__init__("https://training.example")
        self.responses = list(responses)
        self.request_headers = []

    def _request(self, _path_or_url, *, data=None, headers=None, timeout=45):
        self.request_headers.append(headers or {})
        return self.responses.pop(0)


class ParserTests(unittest.TestCase):
    def test_login_command_parses_without_course_id(self):
        args = build_parser().parse_args(["login"])
        self.assertEqual(args.command, "login")
        self.assertFalse(args.manual_session)

    def test_library_commands_parse(self):
        build = build_parser().parse_args(["library", "44", "--output", "my-library", "--reuse-catalog"])
        self.assertEqual(build.course_ids, [44])
        self.assertEqual(build.output, Path("my-library"))
        self.assertTrue(build.reuse_catalog)
        serve = build_parser().parse_args(["serve-library", "--port", "9000"])
        self.assertEqual(serve.port, 9000)

    def test_browser_login_captures_user_agent_and_cookies(self):
        class FakeLaunchLink:
            def is_displayed(self):
                return True

            def get_attribute(self, name):
                if name == "href":
                    return "https://lms.example/moodle/login_student_user/162/"
                return None

        class FakeDriver:
            current_url = "about:blank"

            def get(self, url):
                if "/moodle/login_student_user/" in url:
                    self.current_url = "https://training.example/my/"
                else:
                    self.current_url = url

            def execute_script(self, expression):
                if "navigator.userAgent" in expression:
                    return "Synthetic Browser/1"
                return self.current_url == "https://training.example/my/"

            def find_elements(self, _by, selector):
                if "login_student_user" in selector and self.current_url == "https://lms.example/":
                    return [FakeLaunchLink()]
                return []

            def get_cookies(self):
                return [
                    {"name": "MoodleSession", "value": "synthetic"},
                    {"name": "cf_clearance", "value": "synthetic"},
                ]

            def quit(self):
                return None

        class FakeOptions:
            binary_location = ""

            def add_argument(self, _argument):
                return None

        class FakeService:
            def __init__(self, **_kwargs):
                return None

        fake_webdriver = SimpleNamespace(
            Firefox=lambda **_kwargs: FakeDriver(),
            Chrome=lambda **_kwargs: FakeDriver(),
        )
        fake_selenium = types.ModuleType("selenium")
        fake_selenium.webdriver = fake_webdriver
        fake_common = types.ModuleType("selenium.common")
        fake_exceptions = types.ModuleType("selenium.common.exceptions")
        fake_exceptions.WebDriverException = RuntimeError
        fake_webdriver_module = types.ModuleType("selenium.webdriver")
        fake_webdriver_common = types.ModuleType("selenium.webdriver.common")
        fake_by = types.ModuleType("selenium.webdriver.common.by")
        fake_by.By = SimpleNamespace(CSS_SELECTOR="css selector")
        fake_firefox = types.ModuleType("selenium.webdriver.firefox")
        fake_firefox_options = types.ModuleType("selenium.webdriver.firefox.options")
        fake_firefox_options.Options = FakeOptions
        fake_firefox_service = types.ModuleType("selenium.webdriver.firefox.service")
        fake_firefox_service.Service = FakeService

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "zen"
            executable.touch()
            args = SimpleNamespace(
                site="https://training.example",
                login_url="https://lms.example/",
                browser_profile=Path(directory) / "profile",
                browser_executable=executable,
                login_timeout=30,
                env_values={},
            )
            with patch.dict(
                sys.modules,
                {
                    "selenium": fake_selenium,
                    "selenium.common": fake_common,
                    "selenium.common.exceptions": fake_exceptions,
                    "selenium.webdriver": fake_webdriver_module,
                    "selenium.webdriver.common": fake_webdriver_common,
                    "selenium.webdriver.common.by": fake_by,
                    "selenium.webdriver.firefox": fake_firefox,
                    "selenium.webdriver.firefox.options": fake_firefox_options,
                    "selenium.webdriver.firefox.service": fake_firefox_service,
                },
            ):
                user_agent, cookie_header = _capture_browser_session(args)
        self.assertEqual(user_agent, "Synthetic Browser/1")
        self.assertEqual(
            cookie_header,
            "MoodleSession=synthetic; cf_clearance=synthetic",
        )

    def test_ravin_browser_login_starts_at_account_portal(self):
        args = SimpleNamespace(
            site="https://training.ravinacademy.com",
            login_url=None,
            env_values={},
        )
        self.assertEqual(_browser_login_url(args), "https://lms.ravinacademy.com/")

    def test_parses_login_form_and_hidden_token(self):
        parser = _FormParser()
        parser.feed(
            '<form method="post" action="/login/index.php">'
            '<input type="hidden" name="logintoken" value="abc">'
            '<input name="username"><input name="password"></form>'
        )
        self.assertEqual(parser.forms[0]["inputs"]["logintoken"], "abc")
        self.assertEqual(parser.forms[0]["method"], "post")

    def test_parses_moodle_config(self):
        page = '<script>M.cfg = {"homeurl":{},"sesskey":"sec}ret","userId":1818};</script>'
        self.assertEqual(
            _parse_moodle_config(page),
            {"homeurl": {}, "sesskey": "sec}ret", "userId": 1818},
        )

    def test_collects_links_and_embedded_media(self):
        parser = _LinkParser("https://training.example/mod/resource/view.php?id=1")
        parser.feed(
            '<a href="/pluginfile.php/10/file.pdf"> Notes </a>'
            '<video><source src="/pluginfile.php/11/video.mp4" type="video/mp4"></video>'
        )
        self.assertEqual(parser.links[0], ("https://training.example/pluginfile.php/10/file.pdf", "Notes"))
        self.assertEqual(parser.media[0], "https://training.example/pluginfile.php/11/video.mp4")

    def test_parses_course_chapters_and_activities(self):
        parser = _CourseStructureParser("https://training.example/course/view.php?id=44")
        parser.feed(
            '<li id="section-2" data-for="section" data-id="940" '
            'data-sectionid="2" data-sectionname="Chapter 1">'
            '<input type="checkbox">'
            '<div class="summary"><p>Core networking concepts</p></div>'
            '<ul><li class="activity resource modtype_resource" data-for="cmitem" data-id="4940">'
            '<div data-activityname="Introduction to Networks">'
            '<img src="icon.png"><a href="/mod/resource/view.php?id=4940">Lesson</a>'
            '<span class="activitybadge">MP4</span><button class="btn-subtle-success">Done</button>'
            '</div></li></ul></li>'
        )
        sections = parser.result()
        self.assertEqual(sections[0]["name"], "Chapter 1")
        self.assertEqual(sections[0]["summary"], "Core networking concepts")
        self.assertEqual(sections[0]["activities"][0]["id"], 4940)
        self.assertEqual(sections[0]["activities"][0]["type"], "resource")
        self.assertEqual(sections[0]["activities"][0]["badge"], "MP4")
        self.assertTrue(sections[0]["activities"][0]["lms_completed"])

    def test_filename_prefers_content_disposition(self):
        headers = email.message.Message()
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''lesson%201.mp4"
        self.assertEqual(_filename_from_headers(headers, "https://x/file.php/1", "lesson"), "lesson 1.mp4")

    def test_sanitizes_names_and_deduplicates_files(self):
        self.assertEqual(_clean_name('Week 1: Intro/Setup?'), "Week 1_ Intro_Setup_")
        self.assertEqual(_activity_directory_name(17, 2, 5201), "017--002--5201")
        item = FileItem(44, "Chapter 1", "Intro", "1.mp4", "https://x/pluginfile.php/1/1.mp4?token=a")
        duplicate = FileItem(44, "Other", "Other", "1.mp4", "https://x/pluginfile.php/1/1.mp4?token=b")
        self.assertEqual(_unique([item, duplicate]), [item])

    def test_detects_cloudflare_page(self):
        self.assertTrue(_is_cloudflare_challenge('<link href="/cdn-cgi/assets/css/static.css">'))
        self.assertFalse(_is_cloudflare_challenge("<html><body>Moodle login</body></html>"))

    def test_saves_and_loads_private_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("UNRELATED='keep me'\nRAVIN_USERNAME=old\n", encoding="utf-8")
            _save_env_values(
                path,
                {
                    "RAVIN_USERNAME": "0935",
                    "RAVIN_PASSWORD": 'pass"word',
                    "RAVIN_COOKIE": "example_session=synthetic-value",
                },
            )
            self.assertEqual(
                _load_env_file(path),
                {
                    "UNRELATED": "keep me",
                    "RAVIN_USERNAME": "0935",
                    "RAVIN_PASSWORD": 'pass"word',
                    "RAVIN_COOKIE": "example_session=synthetic-value",
                },
            )
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_builds_static_library_catalog_from_library_content(self):
        class FakeClient:
            site = "https://training.example"

            def list_courses(self):
                return [Course(44, "Network +", "NET-44")]

            def list_files(self, course_id):
                self.assert_course_id = course_id
                return [
                    FileItem(
                        44,
                        "Course files",
                        "Introduction فایل",
                        "1.mp4",
                        "https://training.example/pluginfile.php/10/1.mp4?token=secret",
                        "video/mp4",
                        10,
                        section_id=940,
                        section_number=2,
                        activity_id=4940,
                        activity_type="resource",
                        activity_position=1,
                    ),
                    FileItem(
                        44,
                        "Course files",
                        "Notes فایل",
                        "notes.pdf",
                        "https://training.example/pluginfile.php/11/notes.pdf",
                        "application/pdf",
                        20,
                        section_id=941,
                        section_number=3,
                        activity_id=4941,
                        activity_type="resource",
                        activity_position=1,
                    ),
                ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "library"
            video = output / "courses" / "44" / "content" / "002--001--4940" / "files" / "1.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            catalog = _build_library_catalog(FakeClient(), output)
            self.assertEqual(catalog["stats"]["courses"], 1)
            self.assertEqual(catalog["stats"]["downloaded_files"], 1)
            items = catalog["courses"][0]["sections"][0]["items"]
            self.assertEqual(items[0]["title"], "Introduction")
            self.assertEqual(items[0]["kind"], "video")
            self.assertEqual(items[0]["status"], "downloaded")
            self.assertEqual(items[0]["local_url"], "courses/44/content/002--001--4940/files/1.mp4")
            self.assertEqual(items[1]["status"], "missing")
            catalog_path = _write_library_site(output, catalog)
            written = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"], 3)
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "course.html").is_file())
            self.assertTrue((output / "courses" / "44" / "course.json").is_file())
            nginx_config = (output / "nginx-server.conf").read_text(encoding="utf-8")
            self.assertIn(f"root \"{output.resolve()}\";", nginx_config)
            self.assertNotIn(str(root / ".env"), nginx_config)

    def test_reuses_catalog_and_refreshes_library_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "library"
            media = output / "courses" / "44" / "content" / "002--001--4940" / "files" / "1.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            (media.parent.parent / "item.json").write_text(
                json.dumps({"files": ["files/1.mp4"]}),
                encoding="utf-8",
            )
            (output / "courses.json").write_text(
                json.dumps(
                    {
                        "courses": [
                            {
                                "id": 44,
                                "sections": [
                                    {
                                        "items": [
                                            {
                                                "local_url": None,
                                                "filename": "1.mp4",
                                                "activity_type": "resource",
                                                "title": "Introduction to Networks",
                                                "status": "online",
                                                "section_number": 2,
                                                "activity_position": 1,
                                                "activity_id": 4940,
                                            },
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = _reuse_library_catalog(output)
            item = catalog["courses"][0]["sections"][0]["items"][0]
            self.assertEqual(item["local_url"], "courses/44/content/002--001--4940/files/1.mp4")
            self.assertEqual(item["status"], "downloaded")
            self.assertIn("prepared_at", catalog)

    def test_interrupted_download_retries_with_range(self):
        first = _FakeResponse(
            [
                b"abcde",
                http.client.IncompleteRead(b"xyz", 2),
            ]
        )
        second = _FakeResponse([b"!!"], status=206, content_range="bytes 8-9/10")
        client = _FakeDownloadClient([first, second])
        item = FileItem(
            44, "Course files", "Lesson", "file.bin", "https://training.example/file.bin",
            section_number=2, activity_id=4940, activity_position=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = client.download(item, Path(directory), retries=1, retry_delay=0)
            self.assertEqual(destination.read_bytes(), b"abcdexyz!!")
        self.assertEqual(client.request_headers, [{}, {"Range": "bytes=8-"}])

    def test_existing_partial_download_is_resumed(self):
        response = _FakeResponse([b"67890"], status=206, content_range="bytes 5-9/10")
        client = _FakeDownloadClient([response])
        item = FileItem(
            44, "Course files", "Lesson", "file.bin", "https://training.example/file.bin",
            section_number=2, activity_id=4940, activity_position=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "courses" / "44" / "content" / "002--001--4940" / "files" / "file.bin.part"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"12345")
            destination = client.download(item, Path(directory), retries=0, retry_delay=0)
            self.assertEqual(destination.read_bytes(), b"1234567890")
        self.assertEqual(client.request_headers, [{"Range": "bytes=5-"}])

    def test_migrates_legacy_video_artifacts_and_exam_in_course_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "downloads"
            library = root / "library"
            course_files = source / "44" / "Course files"
            exams = source / "44" / "Course exams"
            course_files.mkdir(parents=True)
            exams.mkdir(parents=True)
            (course_files / "15.mp4").write_bytes(b"video")
            (course_files / "15.mp4.txt").write_text("transcript", encoding="utf-8")
            (course_files / "15.mp4.txt.json").write_text(
                json.dumps({"source": "/private/15.mp4", "transcript": "/private/15.mp4.txt"}),
                encoding="utf-8",
            )
            (course_files / "15.mp4.md").write_text("**منبع:** private/path\n\nSummary", encoding="utf-8")
            (exams / "041264.pdf").write_bytes(b"exam")
            (exams / "041264.md").write_text("# Questions", encoding="utf-8")
            library.mkdir()
            (library / "courses.json").write_text(
                json.dumps(
                    {
                        "courses": [
                            {
                                "id": 44,
                                "sections": [
                                    {
                                        "id": 847,
                                        "number": 16,
                                        "name": "Chapter 15",
                                        "items": [
                                            {
                                                "activity_id": 4954,
                                                "activity_position": 1,
                                                "activity_type": "resource",
                                                "section_id": 847,
                                                "section_number": 16,
                                                "title": "Lesson 15",
                                                "filename": "15.mp4",
                                                "mimetype": "video/mp4",
                                            }
                                        ],
                                    },
                                    {
                                        "id": 924,
                                        "number": 17,
                                        "name": "تمرین اول",
                                        "items": [
                                            {
                                                "activity_id": 5201,
                                                "activity_position": 2,
                                                "activity_type": "quiz",
                                                "section_id": 924,
                                                "section_number": 17,
                                                "title": "تمرین اول",
                                                "filename": "",
                                            }
                                        ],
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = _migrate_legacy_downloads(source, library, [44])
            self.assertEqual(result["courses"][0]["moved_files"], 6)
            lesson = library / "courses" / "44" / "content" / "016--001--4954"
            exam = library / "courses" / "44" / "content" / "017--002--5201"
            self.assertEqual((lesson / "files" / "15.mp4").read_bytes(), b"video")
            metadata = json.loads((lesson / "artifacts" / "transcript.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source"], "../files/15.mp4")
            self.assertEqual(metadata["transcript"], "transcript.fa.txt")
            self.assertTrue((exam / "files" / "041264.pdf").is_file())
            self.assertTrue((exam / "artifacts" / "questions.fa.md").is_file())
            self.assertFalse((source / "44").exists())


if __name__ == "__main__":
    unittest.main()
