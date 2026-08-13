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
from ravin.migration import migrate_library_to_public
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
from ravin.scan import format_scan, scan_offline, scan_remote


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

    def test_all_primary_commands_parse(self):
        scan = build_parser().parse_args(["scan", "44", "--public", "site", "--offline", "--json"])
        self.assertEqual(scan.course_ids, [44])
        self.assertEqual(scan.public, Path("site"))
        self.assertTrue(scan.offline)
        self.assertTrue(scan.json)
        download = build_parser().parse_args(["download", "44", "--retries", "2"])
        self.assertEqual(download.course_id, 44)
        self.assertEqual(download.retries, 2)
        transcribe = build_parser().parse_args(["transcribe", "44", "--model", "small", "--no-keep-awake"])
        self.assertEqual(transcribe.course_ids, [44])
        self.assertEqual(transcribe.model, "small")
        self.assertFalse(transcribe.keep_awake)
        summarize = build_parser().parse_args(["summarize", "44", "--model", "gpt-test"])
        self.assertEqual(summarize.course_ids, [44])
        self.assertEqual(summarize.model, "gpt-test")
        questions = build_parser().parse_args(
            ["questions", "44", "5679", "questions.fa.md", "--file", "5679.pdf"]
        )
        self.assertEqual(questions.course_id, 44)
        self.assertEqual(questions.activity_id, 5679)
        self.assertEqual(questions.questions, Path("questions.fa.md"))
        self.assertEqual(questions.files, [Path("5679.pdf")])
        questions_wizard = build_parser().parse_args(["questions"])
        self.assertIsNone(questions_wizard.course_id)
        self.assertIsNone(questions_wizard.activity_id)
        self.assertIsNone(questions_wizard.questions)
        recording = build_parser().parse_args(["recording", "44", "5097", "class.mp4"])
        self.assertEqual(recording.course_id, 44)
        self.assertEqual(recording.activity_id, 5097)
        self.assertEqual(recording.video, Path("class.mp4"))
        recordings_wizard = build_parser().parse_args(["recordings"])
        self.assertIsNone(recordings_wizard.course_id)
        self.assertIsNone(recordings_wizard.activity_id)
        self.assertIsNone(recordings_wizard.video)
        export = build_parser().parse_args(
            ["export", "--output", "backup.zip", "--include-videos", "--public", "site"]
        )
        self.assertEqual(export.output, Path("backup.zip"))
        self.assertTrue(export.include_videos)
        self.assertEqual(export.public, Path("site"))
        default_export = build_parser().parse_args(["export"])
        self.assertIsNone(default_export.output)
        serve = build_parser().parse_args(["serve", "--port", "9000"])
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

    def test_filename_repairs_utf8_exposed_as_latin1(self):
        headers = email.message.Message()
        expected = "برنامه زمانبندی.pdf"
        headers["Content-Disposition"] = f'attachment; filename="{expected.encode("utf-8").decode("latin-1")}"'
        self.assertEqual(_filename_from_headers(headers, "https://x/file.php/1", "lesson"), expected)

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

    def test_scan_writes_public_catalog_and_course_manifest(self):
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
                        5,
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
            output = root / "public"
            video = output / "courses" / "44" / "content" / "002--001--4940" / "files" / "1.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            catalog = scan_remote(FakeClient(), output)
            self.assertEqual(catalog["stats"]["courses"], 1)
            self.assertEqual(catalog["stats"]["downloaded_files"], 1)
            manifest_path = output / "courses" / "44" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            items = [
                item
                for section in manifest["course"]["sections"]
                for item in section["items"]
            ]
            self.assertEqual(items[0]["title"], "Introduction")
            self.assertEqual(items[0]["kind"], "video")
            self.assertEqual(items[0]["status"], "downloaded")
            self.assertEqual(items[0]["local_url"], "courses/44/content/002--001--4940/files/1.mp4")
            self.assertEqual(items[0]["state"]["download"], "complete")
            self.assertEqual(items[0]["state"]["transcript"], "missing")
            self.assertEqual(items[1]["status"], "missing")
            written = json.loads((output / "courses" / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"], 1)
            self.assertEqual(written["courses"][0]["manifest_url"], "courses/44/manifest.json")
            self.assertFalse((output / "index.html").exists())

    def test_offline_scan_refreshes_local_artifact_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "public"
            media = output / "courses" / "44" / "content" / "002--001--4940" / "files" / "1.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            manifest_path = output / "courses" / "44" / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "course": {
                            "id": 44,
                            "fullname": "Network +",
                            "sections": [{
                                "number": 2,
                                "items": [{
                                    "filename": "1.mp4",
                                    "activity_type": "resource",
                                    "kind": "video",
                                    "title": "Introduction to Networks",
                                    "section_number": 2,
                                    "activity_position": 1,
                                    "activity_id": 4940,
                                }],
                            }],
                        },
                    }
                ),
                encoding="utf-8",
            )
            catalog = scan_offline(output, [44])
            refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = refreshed["course"]["sections"][0]["items"][0]
            self.assertEqual(item["local_url"], "courses/44/content/002--001--4940/files/1.mp4")
            self.assertEqual(item["status"], "downloaded")
            self.assertEqual(item["state"]["download"], "complete")
            self.assertEqual(catalog["stats"]["downloaded_files"], 1)

    def test_offline_scan_uses_the_downloaders_normalized_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            activity = output / "courses" / "44" / "content" / "013--001--4951"
            downloaded = activity / "files" / "Network + 12 Exp.mp4"
            downloaded.parent.mkdir(parents=True)
            downloaded.write_bytes(b"replacement video")
            old_file = activity / "files" / "12.mp4"
            old_file.write_bytes(b"old video preserved")
            transcript = activity / "artifacts" / "transcript.fa.txt"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("old transcript", encoding="utf-8")
            old_stat = old_file.stat()
            (activity / "artifacts" / "transcript.meta.json").write_text(
                json.dumps(
                    {
                        "source": "../files/12.mp4",
                        "source_size": old_stat.st_size,
                        "source_mtime_ns": old_stat.st_mtime_ns,
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output / "courses" / "44" / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "course": {
                            "id": 44,
                            "fullname": "Network +",
                            "sections": [{
                                "number": 13,
                                "items": [{
                                    "filename": "Network  + 12 Exp.mp4",
                                    "activity_type": "resource",
                                    "kind": "video",
                                    "title": "Internet Protocol Addressing (IP)",
                                    "section_number": 13,
                                    "activity_position": 1,
                                    "activity_id": 4951,
                                }],
                            }],
                        },
                    }
                ),
                encoding="utf-8",
            )

            scan_offline(output, [44])

            refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = refreshed["course"]["sections"][0]["items"][0]
            self.assertEqual(item["state"]["download"], "complete")
            self.assertEqual(
                item["local_url"],
                "courses/44/content/013--001--4951/files/Network%20%2B%2012%20Exp.mp4",
            )
            self.assertEqual(item["state"]["transcript"], "stale")
            self.assertEqual(
                [(version["filename"], version["state"]) for version in item["file_versions"]],
                [("Network + 12 Exp.mp4", "current"), ("12.mp4", "archived")],
            )
            self.assertEqual(downloaded.read_bytes(), b"replacement video")
            self.assertEqual(old_file.read_bytes(), b"old video preserved")

    def test_offline_scan_recognizes_legacy_mojibake_and_ignores_empty_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            activity = output / "courses" / "51" / "content" / "001--001--5685"
            expected = "برنامه زمانبندی.pdf"
            legacy = _clean_name(expected.encode("utf-8").decode("latin-1"), "file")
            downloaded = activity / "files" / legacy
            downloaded.parent.mkdir(parents=True)
            downloaded.write_bytes(b"schedule")
            manifest_path = output / "courses" / "51" / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "course": {
                            "id": 51,
                            "fullname": "LPIC",
                            "sections": [{
                                "number": 1,
                                "items": [
                                    {
                                        "filename": expected,
                                        "activity_type": "resource",
                                        "kind": "document",
                                        "title": "Schedule",
                                        "section_number": 1,
                                        "activity_position": 1,
                                        "activity_id": 5685,
                                    },
                                    {
                                        "filename": "",
                                        "activity_type": "resource",
                                        "kind": "resource",
                                        "title": "Empty resource",
                                        "section_number": 1,
                                        "activity_position": 2,
                                        "activity_id": 5686,
                                    },
                                ],
                            }],
                        },
                    }
                ),
                encoding="utf-8",
            )

            catalog = scan_offline(output, [51])

            refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
            first, second = refreshed["course"]["sections"][0]["items"]
            self.assertEqual(first["state"]["download"], "complete")
            self.assertIsNotNone(first["local_url"])
            self.assertEqual(second["state"]["download"], "not_applicable")
            self.assertEqual(refreshed["course"]["states"]["downloads"], {"total": 1, "complete": 1})
            self.assertIn("Downloads      1 / 1   complete", format_scan(catalog))

    def test_scan_output_lists_state_details_and_ordered_next_commands(self):
        catalog = {
            "courses": [{
                "id": 51,
                "fullname": "LPIC",
                "section_count": 2,
                "activity_count": 10,
                "states": {
                    "downloads": {"total": 8, "complete": 7, "missing": 1},
                    "transcripts": {"total": 6, "complete": 4, "missing": 1, "stale": 1},
                    "summaries": {"total": 6, "complete": 0, "missing": 6},
                    "assessments": {"total": 1, "complete": 0, "missing": 1},
                    "recordings": {"total": 2, "complete": 1, "missing": 1},
                    "partial": 0,
                    "stale": 1,
                    "errors": 0,
                },
            }]
        }

        output = format_scan(catalog)

        self.assertIn("Downloads      7 / 8   complete · 1 missing", output)
        self.assertIn("Transcripts    4 / 6   complete · 1 missing, 1 stale", output)
        self.assertLess(output.index("ravin download 51"), output.index("ravin transcribe 51"))
        self.assertLess(output.index("ravin transcribe 51"), output.index("ravin summarize 51"))
        self.assertIn("ravin questions 51", output)
        self.assertIn("ravin recording 51", output)

    def test_changed_same_name_download_archives_previous_file(self):
        response = _FakeResponse([b"new version"])
        client = _FakeDownloadClient([response])
        item = FileItem(
            44,
            "Course files",
            "Lesson",
            "file.bin",
            "https://training.example/file.bin",
            filesize=len(b"new version"),
            section_number=2,
            activity_id=4940,
            activity_position=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "courses" / "44" / "content" / "002--001--4940" / "files" / "file.bin"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")

            downloaded = client.download(item, root, retries=0, retry_delay=0)

            archived = list((destination.parent / "archive").glob("*--file.bin"))
            self.assertEqual(downloaded.read_bytes(), b"new version")
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), b"old")

    def test_download_reuses_legacy_mojibake_path_without_requesting_it_again(self):
        expected = "برنامه زمانبندی.pdf"
        legacy = _clean_name(expected.encode("utf-8").decode("latin-1"), "file")
        client = _FakeDownloadClient([])
        item = FileItem(
            51,
            "Course files",
            "Schedule",
            expected,
            "https://training.example/file.pdf",
            section_number=1,
            activity_id=5685,
            activity_position=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "courses" / "51" / "content" / "001--001--5685" / "files" / legacy
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"schedule")

            reused = client.download(item, root)

            self.assertEqual(reused, destination)
            self.assertEqual(client.request_headers, [])

    def test_public_library_renders_current_and_archived_versions(self):
        script = (Path(__file__).resolve().parents[1] / "public" / "assets" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("current-version-badge", script)
        self.assertIn("archivedVersions", script)
        self.assertIn("Play archived", script)

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

    def test_migrates_existing_library_bundles_into_public(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            public = root / "public"
            lesson = library / "courses" / "44" / "content" / "016--001--4954"
            (lesson / "files").mkdir(parents=True)
            (lesson / "files" / "15.mp4").write_bytes(b"video")
            (lesson / "item.json").write_text("{}", encoding="utf-8")
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
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = migrate_library_to_public(library, public)
            migrated = public / "courses" / "44" / "content" / "016--001--4954"
            self.assertEqual((migrated / "files" / "15.mp4").read_bytes(), b"video")
            self.assertFalse((migrated / "item.json").exists())
            self.assertTrue((public / "courses" / "44" / "manifest.json").is_file())
            self.assertEqual(result["catalog"]["stats"]["downloaded_files"], 1)
            self.assertFalse(library.exists())


if __name__ == "__main__":
    unittest.main()
