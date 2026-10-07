"""Environment configuration and interactive browser authentication."""

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from .client import MoodleClient
from .constants import DEFAULT_RAVIN_LOGIN_URL, ENV_KEYS
from .models import MoodleError


class _InvalidSSOKey(MoodleError):
    """The portal transfer failed before establishing a Moodle session."""


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


def _authenticated_browser_session(driver, site: str) -> tuple[str, str] | None:
    """Capture only a signed-in Moodle page on the configured site."""
    location = urllib.parse.urlsplit(driver.current_url)
    target = urllib.parse.urlsplit(site)
    if (location.scheme, location.netloc.casefold()) != (target.scheme, target.netloc.casefold()):
        return None
    if "/login/" in location.path or location.path.endswith("/auth/userkey/login.php"):
        return None
    logged_in = driver.execute_script(
        """return Boolean(
            window.M && M.cfg && Number(M.cfg.userId) > 0 && M.cfg.sesskey
        );"""
    )
    if not logged_in:
        return None
    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in driver.get_cookies()
        if cookie.get("name") and cookie.get("value")
    )
    if not cookie_header:
        raise MoodleError("browser login succeeded, but no site cookies were available")
    return str(driver.execute_script("return navigator.userAgent;")), cookie_header


def _select_login_tab(driver, known_handles: set[str]) -> set[str]:
    """Follow a newly opened login tab, or recover when the active tab closes."""
    from selenium.common.exceptions import NoSuchWindowException

    handles = driver.window_handles
    if not handles:
        raise MoodleError("the authentication browser was closed before login finished")
    new_handles = [handle for handle in handles if handle not in known_handles]
    if new_handles:
        driver.switch_to.window(new_handles[-1])
    else:
        try:
            current = driver.current_window_handle
        except NoSuchWindowException:
            current = None
        if current not in handles:
            driver.switch_to.window(handles[-1])
    return set(handles)


def _launch_moodle_from_portal(driver, login_url: str, by) -> bool:
    """Activate the actual portal link so browser navigation preserves its context."""
    links = driver.find_elements(by.CSS_SELECTOR, 'a[href*="/moodle/login_student_user/"]')
    candidates = [
        link for link in links
        if urllib.parse.urlsplit(link.get_attribute("href") or "").hostname
        == urllib.parse.urlsplit(login_url).hostname
    ]
    if not candidates:
        return False
    visible = next((link for link in candidates if link.is_displayed()), None)
    if visible is not None:
        visible.click()
    else:
        # Closed dropdowns still contain the real link and its event handlers.
        driver.execute_script("arguments[0].click();", candidates[0])
    return True


def _capture_chromium_session(args: argparse.Namespace, portal_cookies=()) -> tuple[str, str]:
    """Use a persistent Chromium context when Firefox's portal transfer fails."""
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError as exc:
        raise MoodleError("Chromium login requires Playwright; reinstall the updated package") from exc

    profile = args.browser_profile.expanduser().resolve() / "chromium"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + max(args.login_timeout, 30)
    login_url = _browser_login_url(args)
    target = urllib.parse.urlsplit(args.site)
    saved = _browser_session_values(args)
    # A browser restart may discard session-only cookies. Rehydrate only a
    # previous Chromium session, using its matching User-Agent.
    chromium_saved = saved if saved and "Chrome/" in saved[0] and not portal_cookies else None
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            raise MoodleError("Chromium is not installed. Run `python -m playwright install chromium` and retry login")
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False,
                **({"user_agent": chromium_saved[0]} if chromium_saved else {}),
            )
            try:
                if chromium_saved:
                    context.add_cookies([
                        {"name": name.strip(), "value": value, "url": args.site.rstrip("/") + "/", "secure": target.scheme == "https"}
                        for pair in chromium_saved[1].split(";")
                        for name, separator, value in [pair.strip().partition("=")]
                        if separator and name.strip()
                    ])
                if portal_cookies:
                    # Keep the authorized portal session when changing engines.
                    # Analytics cookies are unnecessary and can have invalid flags.
                    cookies = []
                    for cookie in portal_cookies:
                        if cookie["name"].startswith(("_ga", "_gid", "_gat")):
                            continue
                        copied = {key: cookie[key] for key in ("name", "value", "domain", "path")}
                        copied.update(secure=cookie.get("secure", False), httpOnly=cookie.get("httpOnly", False))
                        if cookie.get("expiry"):
                            copied["expires"] = cookie["expiry"]
                        cookies.append(copied)
                    context.add_cookies(cookies)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(args.site.rstrip("/") + "/my/courses.php")
                launched = False
                opened_portal = False
                known_pages = set(context.pages)
                while time.monotonic() < deadline:
                    pages = [item for item in context.pages if not item.is_closed()]
                    if not pages:
                        raise MoodleError("the authentication browser was closed before login finished")
                    new_pages = [item for item in pages if item not in known_pages]
                    if new_pages or page.is_closed():
                        page = (new_pages or pages)[-1]
                    known_pages = set(pages)
                    location = urllib.parse.urlsplit(page.url)
                    if (location.scheme, location.netloc.casefold()) == (target.scheme, target.netloc.casefold()):
                        if page.locator('a[href*="/error/moodle/invalidkey"]').count():
                            raise _InvalidSSOKey("Moodle rejected the portal sign-in key in Chromium too; no new session was saved")
                        if "/login/" not in location.path and page.evaluate(
                            "Boolean(window.M && M.cfg && Number(M.cfg.userId) > 0 && M.cfg.sesskey)"
                        ):
                            cookie_header = "; ".join(
                                f"{cookie['name']}={cookie['value']}" for cookie in context.cookies(page.url)
                                if cookie.get("name") and cookie.get("value")
                            )
                            if not cookie_header:
                                raise MoodleError("Chromium login succeeded, but no cookies were available")
                            return page.evaluate("navigator.userAgent"), cookie_header
                    if not opened_portal:
                        page.goto(login_url)
                        opened_portal = True
                        continue
                    if not launched and location.hostname == urllib.parse.urlsplit(login_url).hostname:
                        links = page.locator('a[href*="/moodle/login_student_user/"]').all()
                        links = [link for link in links if urllib.parse.urlsplit(
                            urllib.parse.urljoin(page.url, link.get_attribute("href") or "")
                        ).hostname == location.hostname]
                        if links:
                            visible = next((link for link in links if link.is_visible()), None)
                            if visible is not None:
                                visible.click()
                            else:
                                links[0].evaluate("element => element.click()")
                            launched = True
                            print("Opening the Moodle course portal in Chromium...", file=sys.stderr)
                            continue
                    time.sleep(1)
                raise MoodleError(f"Chromium login did not finish within {max(args.login_timeout, 30)} seconds")
            finally:
                context.close()
        except PlaywrightError as exc:
            # Browser exceptions can embed the one-time login URL.
            raise MoodleError("Chromium authentication could not complete; check the browser or retry login") from None


def _capture_browser_session(args: argparse.Namespace) -> tuple[str, str]:
    """Open an installed browser through Selenium and capture its authenticated session."""
    try:
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise MoodleError(
            "automatic browser login is not installed. Run:\n"
            "  python3 -m pip install ravin-moodle-downloader"
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

    if not args.browser_executable and (profile / "chromium" / "Local State").is_file():
        print("Opening the previously verified Chromium login profile...", file=sys.stderr)
        return _capture_chromium_session(args)

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
        # A persistent profile may already hold a valid Moodle session. Reuse it
        # before issuing another single-sign-on launch from the account portal.
        driver.get(args.site.rstrip("/") + "/my/courses.php")
        session = _authenticated_browser_session(driver, args.site)
        if session is not None:
            return session
        driver.get(login_url)
        known_handles = set(driver.window_handles)
        launched_moodle = False
        submitted_credentials = False
        credential_attempts = 0
        captcha_notice_shown = False
        last_location = ""
        while time.monotonic() < deadline:
            known_handles = _select_login_tab(driver, known_handles)
            current_location = urllib.parse.urlsplit(driver.current_url)._replace(query="", fragment="").geturl()
            if current_location != last_location:
                print(f"Browser is at {current_location}", file=sys.stderr)
                last_location = current_location
            try:
                session = _authenticated_browser_session(driver, args.site)
            except WebDriverException:
                session = None
            if session is not None:
                return session

            if driver.find_elements(By.CSS_SELECTOR, 'a[href*="/error/moodle/invalidkey"]'):
                raise _InvalidSSOKey(
                    "Moodle rejected the portal's sign-in key (invalidkey). "
                    "The browser did not establish a session; no new session was saved. "
                    "The account portal's single-sign-on may need attention."
                )

            if launched_moodle:
                # A click may open its destination asynchronously. Do not issue
                # another launch or submit the portal form during that transfer.
                time.sleep(1)
                continue

            location = urllib.parse.urlsplit(driver.current_url)
            allowed_origins = {
                (parsed.scheme, parsed.netloc.casefold())
                for parsed in (urllib.parse.urlsplit(args.site), urllib.parse.urlsplit(login_url))
            }
            if (location.scheme, location.netloc.casefold()) not in allowed_origins:
                time.sleep(1)
                continue

            # Ravin's account portal owns the login. Its Moodle launch link creates
            # the session on training.ravinacademy.com and redirects there.
            if not launched_moodle and _launch_moodle_from_portal(driver, login_url, By):
                print("LMS login accepted; opening the Moodle course portal.", file=sys.stderr)
                launched_moodle = True
                submitted_credentials = False
                continue

            # The portal now requires a human-entered CAPTCHA. Leave the form
            # untouched rather than submitting incomplete credentials repeatedly.
            captcha_controls = driver.find_elements(
                By.CSS_SELECTOR,
                'input[name="captcha_1"], input[name="g-recaptcha-response"], '
                '.g-recaptcha, .h-captcha, iframe[src*="recaptcha"], iframe[src*="hcaptcha"]',
            )
            if any(element.is_displayed() for element in captcha_controls):
                if not captcha_notice_shown:
                    print("CAPTCHA detected; complete login in the browser window.", file=sys.stderr)
                    captcha_notice_shown = True
                time.sleep(1)
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
    except _InvalidSSOKey:
        if not ("firefox" in browser_name or "zen" in browser_name):
            raise
        print("Firefox/Zen rejected the portal transfer; switching to Chromium.", file=sys.stderr)
        driver.get(login_url)
        portal_cookies = driver.get_cookies()
        return _capture_chromium_session(args, portal_cookies)
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
