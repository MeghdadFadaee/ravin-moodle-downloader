import email.message
import http.client
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ravin_downloader import (
    FileItem,
    MoodleClient,
    _FormParser,
    _LinkParser,
    _clean_name,
    _browser_login_url,
    _capture_browser_session,
    _filename_from_headers,
    _is_cloudflare_challenge,
    _load_env_file,
    _parse_moodle_config,
    _save_env_values,
    _unique,
    build_parser,
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

    def test_filename_prefers_content_disposition(self):
        headers = email.message.Message()
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''lesson%201.mp4"
        self.assertEqual(_filename_from_headers(headers, "https://x/file.php/1", "lesson"), "lesson 1.mp4")

    def test_sanitizes_names_and_deduplicates_files(self):
        self.assertEqual(_clean_name('Week 1: Intro/Setup?'), "Week 1_ Intro_Setup_")
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

    def test_interrupted_download_retries_with_range(self):
        first = _FakeResponse(
            [
                b"abcde",
                http.client.IncompleteRead(b"xyz", 2),
            ]
        )
        second = _FakeResponse([b"!!"], status=206, content_range="bytes 8-9/10")
        client = _FakeDownloadClient([first, second])
        item = FileItem(44, "Course files", "Lesson", "file.bin", "https://training.example/file.bin")
        with tempfile.TemporaryDirectory() as directory:
            destination = client.download(item, Path(directory), retries=1, retry_delay=0)
            self.assertEqual(destination.read_bytes(), b"abcdexyz!!")
        self.assertEqual(client.request_headers, [{}, {"Range": "bytes=8-"}])

    def test_existing_partial_download_is_resumed(self):
        response = _FakeResponse([b"67890"], status=206, content_range="bytes 5-9/10")
        client = _FakeDownloadClient([response])
        item = FileItem(44, "Course files", "Lesson", "file.bin", "https://training.example/file.bin")
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "44" / "Course files" / "file.bin.part"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"12345")
            destination = client.download(item, Path(directory), retries=0, retry_delay=0)
            self.assertEqual(destination.read_bytes(), b"1234567890")
        self.assertEqual(client.request_headers, [{"Range": "bytes=5-"}])


if __name__ == "__main__":
    unittest.main()
