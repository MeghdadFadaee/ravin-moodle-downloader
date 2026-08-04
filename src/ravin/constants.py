"""Project-wide defaults and version information."""

from pathlib import Path


DEFAULT_SITE = "https://training.ravinacademy.com"
DEFAULT_RAVIN_LOGIN_URL = "https://lms.ravinacademy.com/"
__version__ = "0.7.0"
USER_AGENT = f"RavinMoodleDownloader/{__version__} (+personal Moodle client)"
DOWNLOADABLE_MODULES = {"resource", "folder", "page", "book"}
DEFAULT_ENV_FILE = Path.cwd() / ".env"
ENV_KEYS = {
    "username": "RAVIN_USERNAME",
    "password": "RAVIN_PASSWORD",
    "login_url": "RAVIN_LOGIN_URL",
    "user_agent": "RAVIN_USER_AGENT",
    "cookie": "RAVIN_COOKIE",
}
