"""Regressions for the Ravin portal and its October 2026 Moodle theme."""
import email.message
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravin.auth import _capture_browser_session, _authenticated_browser_session, _select_login_tab
from ravin.client import MoodleClient
from ravin.models import MoodleError


class BrowserUpdateTests(unittest.TestCase):
    def capture(self, *, captcha=False, existing_session=False, new_tab=False, invalid_key=False, use_zen=False):
        test = self

        class Element:
            def __init__(self, visible):
                self.visible = visible

            def is_displayed(self):
                return self.visible

            def get_attribute(self, name):
                return 'https://lms.example/moodle/login_student_user/162/' if name == 'href' else None

            def click(self):
                driver.launch()

        class Driver:
            current_url = 'about:blank'
            awaiting_captcha = captcha
            quit_called = False
            launches = 0

            def __init__(self):
                self.window_handles = ['portal']
                self.current_window_handle = 'portal'
                self.switch_to = SimpleNamespace(window=self.switch_window)

            def switch_window(self, handle):
                self.current_window_handle = handle
                self.current_url = 'https://training.example/my/'

            def launch(self):
                self.launches += 1
                test.assertEqual(self.launches, 1, 'SSO link must only be activated once')
                if new_tab:
                    self.window_handles.append('moodle')
                elif invalid_key:
                    self.current_url = 'https://training.example/auth/userkey/login.php?key=synthetic'
                else:
                    self.current_url = 'https://training.example/my/'

            def get(self, url):
                test.assertNotIn('login_student_user', url, 'Use the portal link instead of direct navigation')
                self.current_url = url

            def execute_script(self, script, *args):
                if script == 'arguments[0].click();':
                    args[0].click()
                    return None
                test.assertFalse(args, 'CAPTCHA form must not be auto-submitted')
                if 'navigator.userAgent' in script:
                    return 'Synthetic Browser/1'
                test.assertNotIn('logout.php', script)
                test.assertIn('M.cfg.sesskey', script)
                return self.current_url == 'https://training.example/my/' or (
                    existing_session and self.current_url == 'https://training.example/my/courses.php'
                )

            def find_elements(self, by, selector):
                if 'invalidkey' in selector:
                    return [Element(True)] if '/auth/userkey/' in self.current_url else []
                if 'login_student_user' in selector:
                    return [] if self.awaiting_captcha else [Element(False)]
                if 'captcha' in selector and self.awaiting_captcha:
                    return [Element(True)]
                return []

            def get_cookies(self):
                return [{'name': 'MoodleSession', 'value': 'synthetic'}]

            def quit(self):
                self.quit_called = True

        driver = Driver()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                site='https://training.example', login_url='https://lms.example/',
                browser_executable=None, browser_profile=Path(directory) / 'profile',
                login_timeout=30, env_values={'RAVIN_USERNAME': 'synthetic', 'RAVIN_PASSWORD': 'synthetic'},
            )
            with patch('ravin.auth._find_browser_executable', return_value=Path('/synthetic/zen' if use_zen else '/synthetic/chrome')), \
                 patch('selenium.webdriver.Chrome', return_value=driver), \
                 patch('selenium.webdriver.Firefox', return_value=driver), \
                 patch('selenium.webdriver.firefox.service.Service'), \
                 patch('ravin.auth._capture_chromium_session', return_value=('Synthetic Browser/1', 'MoodleSession=synthetic')) as fallback, \
                 patch('ravin.auth.time.sleep', side_effect=lambda _: setattr(driver, 'awaiting_captcha', False)), \
                 redirect_stderr(output):
                if invalid_key and not use_zen:
                    with self.assertRaisesRegex(MoodleError, 'invalidkey'):
                        _capture_browser_session(args)
                    self.assertTrue(driver.quit_called)
                    self.assertEqual(driver.launches, 1)
                    self.assertNotIn('key=synthetic', output.getvalue())
                    return output.getvalue()
                result = _capture_browser_session(args)
                if invalid_key and use_zen:
                    fallback.assert_called_once_with(args, [{'name': 'MoodleSession', 'value': 'synthetic'}])
                else:
                    fallback.assert_not_called()
        self.assertTrue(driver.quit_called)
        self.assertEqual(result, ('Synthetic Browser/1', 'MoodleSession=synthetic'))
        self.assertEqual(driver.launches, 0 if existing_session else 1)
        return output.getvalue()

    def test_hidden_launch_link_and_theme_without_logout_link(self):
        self.capture()

    def test_captcha_waits_for_human_without_submitting_stored_credentials(self):
        self.assertIn('CAPTCHA detected', self.capture(captcha=True))

    def test_existing_profile_session_skips_portal_launch(self):
        self.capture(existing_session=True)

    def test_new_login_tab_is_followed_without_relaunching(self):
        self.capture(new_tab=True)

    def test_invalid_key_is_reported_without_replaying_the_link(self):
        self.capture(invalid_key=True)

    def test_zen_invalid_key_switches_engine_with_portal_session(self):
        self.assertIn('switching to Chromium', self.capture(invalid_key=True, use_zen=True))

    def test_session_from_another_origin_is_not_captured(self):
        driver = SimpleNamespace(current_url='https://another.example/my/')
        self.assertIsNone(_authenticated_browser_session(driver, 'https://training.example'))

    def test_closed_browser_reports_actionable_error(self):
        with self.assertRaisesRegex(MoodleError, 'browser was closed'):
            _select_login_tab(SimpleNamespace(window_handles=[]), {'portal'})

    def test_closed_active_tab_switches_to_remaining_tab(self):
        from selenium.common.exceptions import NoSuchWindowException

        class Driver:
            window_handles = ['moodle']

            @property
            def current_window_handle(self):
                raise NoSuchWindowException()

        switched = []
        driver = Driver()
        driver.switch_to = SimpleNamespace(window=switched.append)
        self.assertEqual(_select_login_tab(driver, {'moodle', 'portal'}), {'moodle'})
        self.assertEqual(switched, ['moodle'])


class ScanUpdateTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleClient('https://training.example', cookie_header='MoodleSession=synthetic')

    def test_browser_session_accepts_new_theme_without_logout_link(self):
        page = '<script>M.cfg = {"sesskey":"synthetic","userId":42};</script>'
        with patch.object(self.client, '_read_text', return_value=(page, 'https://training.example/my/courses.php', None)):
            self.assertEqual(self.client.authenticate(), 'browser-session')

    def test_guest_session_with_sesskey_is_rejected(self):
        for user in (0, -1, None):
            page = '<script>M.cfg = ' + json.dumps({'sesskey': 'synthetic', 'userId': user}) + ';</script>'
            with self.subTest(user=user), patch.object(self.client, '_read_text', return_value=(page, 'https://training.example/my/courses.php', None)):
                with self.assertRaises(MoodleError):
                    self.client.authenticate()

    def test_login_or_unrecognized_page_cannot_become_empty_course(self):
        for page, url in (
            ('<form><input name="password" type="password"></form>', 'https://lms.example/employee/user/login/'),
            ('<h1>Access denied</h1>', 'https://training.example/course/view.php?id=51'),
        ):
            with self.subTest(url=url), patch.object(self.client, '_read_text', return_value=(page, url, None)):
                with self.assertRaises(MoodleError):
                    self.client.course_structure(51)
                self.assertNotIn(51, self.client._course_structure_cache)

    def test_unexpected_course_response_is_rejected(self):
        for payload in (None, {}, {'courses': None}):
            with self.subTest(payload=payload), patch.object(self.client, 'ajax_call', return_value=payload):
                with self.assertRaises(MoodleError):
                    self.client.list_courses()

    def test_expired_activity_page_aborts_file_discovery(self):
        page = '''<li id="section-1" data-for="section" data-id="1016" data-sectionid="1">
        <li class="modtype_resource" data-for="cmitem" data-id="5685">
        <a href="/mod/resource/view.php?id=5685">Schedule</a></li></li>'''
        headers = email.message.Message()
        headers['Content-Type'] = 'text/html'
        with patch.object(self.client, '_read_text', side_effect=[
            (page, 'https://training.example/course/view.php?id=51', headers),
            ('<form><input name="password"></form>', 'https://training.example/login/index.php', headers),
        ]):
            with self.assertRaisesRegex(MoodleError, 'session expired'):
                self.client.list_files(51)

    def test_collapsed_section_and_sidebar_are_parsed_without_duplicates(self):
        page = '''<div id="course-index-section-1016" data-for="section" data-id="1016" data-number="1">
        <li data-for="cm" data-id="5685"><a href="/mod/resource/view.php?id=5685">Schedule</a></li></div>
        <li id="section-1" data-for="section" data-id="1016" data-sectionid="1" data-sectionname="Schedule">
        <div class="content collapse"><ul data-for="cmlist">
        <li id="module-5685" class="activity modtype_resource" data-for="cmitem" data-id="5685">
        <div data-activityname="Schedule PDF"><a href="/mod/resource/view.php?id=5685">Schedule PDF</a>
        <span class="activitybadge">PDF</span><button class="btn-subtle-success">Done</button></div>
        </li></ul></div></li>'''
        with patch.object(self.client, '_read_text', return_value=(page, 'https://training.example/course/view.php?id=51', None)):
            sections = self.client.course_structure(51)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]['number'], 1)
        self.assertEqual(sections[0]['activities'][0]['id'], 5685)
        self.assertEqual(sections[0]['activities'][0]['name'], 'Schedule PDF')
        self.assertTrue(sections[0]['activities'][0]['lms_completed'])


class ChromiumSessionTests(unittest.TestCase):
    def capture(self, *, portal_cookies=(), invalid_key=False):
        from unittest.mock import MagicMock
        from ravin.auth import _capture_chromium_session
        page = MagicMock()
        page.url = 'https://training.example/my/courses.php'
        page.is_closed.return_value = False
        page.locator.return_value.count.return_value = int(invalid_key)
        page.evaluate.side_effect = lambda script: 'Synthetic Chrome/140' if 'navigator.userAgent' in script else True
        context = MagicMock()
        context.pages = [page]
        context.cookies.return_value = [{'name': 'MoodleSession', 'value': 'synthetic'}]
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                site='https://training.example', login_url='https://lms.example/',
                browser_profile=Path(directory), login_timeout=30,
                browser_user_agent=None,
                env_values={'RAVIN_USER_AGENT': 'Synthetic Chrome/140', 'RAVIN_COOKIE': 'MoodleSession=synthetic=part'},
            )
            with patch('playwright.sync_api.sync_playwright') as runtime, patch('pathlib.Path.is_file', return_value=True):
                chromium = runtime.return_value.__enter__.return_value.chromium
                chromium.launch_persistent_context.return_value = context
                if invalid_key:
                    with self.assertRaisesRegex(MoodleError, 'Chromium too'):
                        _capture_chromium_session(args, portal_cookies)
                else:
                    self.assertEqual(_capture_chromium_session(args, portal_cookies), ('Synthetic Chrome/140', 'MoodleSession=synthetic'))
                context.close.assert_called_once()
                return context, chromium

    def test_browser_restart_rehydrates_saved_session_with_matching_user_agent(self):
        context, chromium = self.capture()
        context.add_cookies.assert_called_once_with([
            {'name': 'MoodleSession', 'value': 'synthetic=part', 'url': 'https://training.example/', 'secure': True}
        ])
        self.assertEqual(chromium.launch_persistent_context.call_args.kwargs['user_agent'], 'Synthetic Chrome/140')

    def test_portal_session_retains_expiry_and_excludes_analytics(self):
        context, chromium = self.capture(portal_cookies=[
            {'name': 'sessionid', 'value': 'synthetic', 'domain': 'lms.example', 'path': '/', 'expiry': 1999999999, 'secure': True, 'httpOnly': True},
            {'name': '_ga', 'value': 'synthetic', 'domain': 'lms.example', 'path': '/'},
        ])
        cookies = context.add_cookies.call_args.args[0]
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]['expires'], 1999999999)
        self.assertTrue(cookies[0]['secure'])
        self.assertTrue(cookies[0]['httpOnly'])
        self.assertNotIn('user_agent', chromium.launch_persistent_context.call_args.kwargs)

    def test_invalid_key_in_chromium_closes_context_without_accepting_session(self):
        self.capture(invalid_key=True)


if __name__ == '__main__':
    unittest.main()
