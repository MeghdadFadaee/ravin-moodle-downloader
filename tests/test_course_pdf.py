from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravin.course_pdf import create_course_pdf, render_html, source_digest, summary_sources
from ravin.models import MoodleError
from ravin.scan import scan_offline
from ravin.cli import build_parser


class CoursePdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.public = Path(self.temp.name)
        self.path = self.public / 'courses/44/content/001--001--101/artifacts/summary.fa.md'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('# شبکه\n\nاعداد ۱۲۳ و ١٢٣ و 123، IP 192.168.1.1\n\n| نام | مقدار |\n| --- | --- |\n| پورت | 8080 |\n\n<script>alert(1)</script>\n', encoding='utf-8')
        self.course = {'id': 44, 'fullname': 'شبکه Network+', 'sections': [
            {'number': 1, 'name': 'فصل اول', 'items': [
                {'title': 'درس اول', 'activity_id': 101, 'activity_position': 1, 'section_number': 1,
                 'artifacts': {'summary': {'url': self.path.relative_to(self.public).as_posix()}}}]}]}
        (self.public / 'courses/44/manifest.json').write_text(json.dumps({'course': self.course}), encoding='utf-8')

    def test_unicode_and_safe_markdown(self):
        try:
            html = render_html(self.course, summary_sources(self.public, self.course))
        except ImportError:
            self.skipTest('PDF optional dependencies are not installed')
        for text in ['۱۲۳', '١٢٣', '123', '192.168.1.1', 'Vazirmatn', '<table>', 'فصل اول']:
            self.assertIn(text, html)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_manifest_pdf_discovery_and_freshness(self):
        scan_offline(self.public, [44])
        course = json.loads((self.public / 'courses/44/manifest.json').read_text())['course']
        target = self.public / 'courses/44/artifacts/summaries.fa.pdf'
        target.parent.mkdir()
        target.write_bytes(b'%PDF-test')
        target.with_suffix('.meta.json').write_text(json.dumps({
            'source_sha256': source_digest(course, summary_sources(self.public, course)), 'summary_count': 1}))
        catalog = scan_offline(self.public, [44])
        self.assertEqual(catalog['courses'][0]['summary_pdf']['state'], 'complete')
        self.path.write_text('خلاصه جدید', encoding='utf-8')
        self.assertEqual(scan_offline(self.public, [44])['courses'][0]['summary_pdf']['state'], 'stale')

    def test_empty_course(self):
        self.path.unlink()
        with self.assertRaisesRegex(MoodleError, 'no non-empty summaries'):
            create_course_pdf(self.public, 44)

    def test_no_path_escape(self):
        self.course['sections'][0]['items'][0]['artifacts']['summary']['url'] = '../outside.md'
        with self.assertRaises(MoodleError):
            summary_sources(self.public, self.course)

    def test_cli(self):
        args = build_parser().parse_args(['pdf', '44', '--json'])
        self.assertEqual(args.course_id, 44)
        self.assertTrue(args.json)

    def test_render_failure_preserves_old_pdf(self):
        try:
            from playwright.sync_api import Error
        except ImportError:
            self.skipTest('PDF optional dependencies are not installed')
        target = self.public / 'courses/44/artifacts/summaries.fa.pdf'
        target.parent.mkdir()
        target.write_bytes(b'old PDF')
        with patch('playwright.sync_api.sync_playwright', side_effect=Error('failed')):
            with self.assertRaises(MoodleError):
                create_course_pdf(self.public, 44)
        self.assertEqual(target.read_bytes(), b'old PDF')
