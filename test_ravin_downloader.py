import email.message
import unittest

from ravin_downloader import (
    FileItem,
    _FormParser,
    _LinkParser,
    _clean_name,
    _filename_from_headers,
    _parse_moodle_config,
    _unique,
)


class ParserTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
