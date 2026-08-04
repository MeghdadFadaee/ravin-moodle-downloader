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
