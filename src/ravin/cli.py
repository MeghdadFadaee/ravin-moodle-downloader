"""Command-line parser and application entry point."""

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from .auth import (
    _authenticate_browser_session,
    _browser_session_values,
    _credentials,
    _load_env_file,
    _save_env_values,
)
from .client import MoodleClient
from .constants import DEFAULT_ENV_FILE, DEFAULT_RAVIN_LOGIN_URL, DEFAULT_SITE, ENV_KEYS, __version__
from .library import (
    _build_library_catalog,
    _format_size,
    _migrate_legacy_downloads,
    _reuse_library_catalog,
    _serve_library,
    _write_library_site,
)
from .models import MoodleError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List and download your Ravin Academy Moodle course files.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--site", default=DEFAULT_SITE, help=f"Moodle base URL (default: {DEFAULT_SITE})")
    parser.add_argument(
        "--login-url",
        help=(
            "account portal used for browser login; defaults to "
            f"{DEFAULT_RAVIN_LOGIN_URL} for Ravin Academy"
        ),
    )
    parser.add_argument("--username", help="LMS username; password is prompted securely")
    parser.add_argument("--web-only", action="store_true", help="skip the Moodle mobile API and use a normal web session")
    parser.add_argument(
        "--browser-session",
        action="store_true",
        help="force saved or automatic browser-session authentication",
    )
    parser.add_argument(
        "--browser-user-agent",
        help="User-Agent copied from the logged-in browser; otherwise prompted",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"credentials and browser-session file (default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--refresh-session",
        action="store_true",
        help="ignore the saved browser session and open the authentication browser",
    )
    parser.add_argument(
        "--manual-session",
        action="store_true",
        help="prompt for User-Agent and Cookie headers instead of opening a browser",
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=Path.cwd() / ".ravin-browser-profile",
        help="persistent profile for automatic browser login",
    )
    parser.add_argument(
        "--browser-executable",
        type=Path,
        help="installed Zen, Firefox, Chrome, Chromium, or Brave executable",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=600,
        help="seconds to wait for interactive browser login (default: 600)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="open a browser and refresh the saved LMS session")
    subparsers.add_parser("courses", help="list enrolled courses")
    files_parser = subparsers.add_parser("files", help="list downloadable files in a course")
    files_parser.add_argument("course_id", type=int)

    download_parser = subparsers.add_parser("download", help="download files from one course")
    download_parser.add_argument("course_id", type=int)
    download_parser.add_argument("--output", type=Path, default=Path("library"), help="library root (default: library)")
    download_parser.add_argument("--overwrite", action="store_true")
    download_parser.add_argument("--retries", type=int, default=5, help="retry interrupted files (default: 5)")
    library_parser = subparsers.add_parser(
        "library",
        help="generate a private static course library and courses.json",
    )
    library_parser.add_argument("course_ids", nargs="*", type=int, help="optional course IDs; defaults to all courses")
    library_parser.add_argument("--output", type=Path, default=Path("library"), help="generated site directory")
    library_parser.add_argument(
        "--reuse-catalog",
        action="store_true",
        help="refresh local files and pages without contacting the LMS",
    )
    migrate_parser = subparsers.add_parser("migrate-library", help="move legacy downloads into the library layout")
    migrate_parser.add_argument("course_ids", nargs="*", type=int, help="optional course IDs; defaults to all catalog courses")
    migrate_parser.add_argument("--source", type=Path, default=Path("downloads"), help="legacy download root")
    migrate_parser.add_argument("--library", type=Path, default=Path("library"), help="library root")
    serve_parser = subparsers.add_parser("serve-library", help="serve the generated library locally")
    serve_parser.add_argument("--site-dir", type=Path, default=Path("library"), help="generated site directory")
    serve_parser.add_argument("--host", default="127.0.0.1", help="local bind address (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8765, help="local port (default: 8765)")
    serve_parser.add_argument("--open", action="store_true", help="open the library in your default browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve-library":
            _serve_library(args.site_dir, args.host, args.port, args.open)
            return 0
        if args.command == "migrate-library":
            source = args.source.expanduser().resolve()
            library = args.library.expanduser().resolve()
            result = _migrate_legacy_downloads(source, library, args.course_ids)
            catalog = _reuse_library_catalog(library)
            catalog_path = _write_library_site(library, catalog)
            moved = sum(int(course.get("moved_files") or 0) for course in result.get("courses", []))
            print(f"Migrated {moved} files and refreshed {catalog_path}.")
            return 0
        if args.command == "library" and args.reuse_catalog:
            output = args.output.expanduser().resolve()
            catalog = _reuse_library_catalog(output)
            catalog_path = _write_library_site(output, catalog)
            print(f"Prepared {catalog_path}, static pages, and Nginx configuration from existing data.")
            return 0
        args.env_values = _load_env_file(args.env_file)
        if args.command == "login":
            client, mode = _authenticate_browser_session(args)
            print(f"Authenticated using {mode}; updated {args.env_file}.")
            return 0
        saved_session = _browser_session_values(args, fresh=args.refresh_session)
        if args.browser_session or args.refresh_session or saved_session is not None:
            client, mode = _authenticate_browser_session(args, saved_session)
        else:
            username, password = _credentials(args)
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
            client = MoodleClient(args.site, username, password, web_only=args.web_only)
            try:
                mode = client.authenticate()
            except MoodleError as exc:
                if "cloudflare requires a real browser session" not in str(exc).casefold():
                    raise
                print("Cloudflare blocked password login; opening the authentication browser.", file=sys.stderr)
                client, mode = _authenticate_browser_session(args)
        print(f"Authenticated using {mode}.", file=sys.stderr)

        if args.command == "courses":
            courses = client.list_courses()
            if args.json:
                print(json.dumps([asdict(course) for course in courses], ensure_ascii=False, indent=2))
            else:
                for course in courses:
                    suffix = f" [{course.shortname}]" if course.shortname else ""
                    print(f"{course.id:>6}  {course.fullname}{suffix}")
            return 0

        if args.command == "library":
            output = args.output.expanduser().resolve()
            catalog = _build_library_catalog(client, output, args.course_ids)
            catalog_path = _write_library_site(output, catalog)
            print(f"Generated {catalog_path} and the static library pages.")
            print("View it with: ravin serve-library --open")
            return 0

        files = client.list_files(args.course_id)
        if args.command == "files":
            if args.json:
                print(json.dumps([asdict(item) for item in files], ensure_ascii=False, indent=2))
            else:
                for number, item in enumerate(files, 1):
                    print(f"{number:>3}. {_format_size(item.filesize):>10}  {item.section} / {item.activity} / {item.filename}")
            return 0

        if not files:
            print("No downloadable files were found.")
            return 0
        downloaded: list[str] = []
        for number, item in enumerate(files, 1):
            print(f"[{number}/{len(files)}] {item.activity}: {item.filename}", file=sys.stderr)
            library = args.output.expanduser().resolve()
            path = client.download(
                item,
                library,
                overwrite=args.overwrite,
                retries=max(args.retries, 0),
            )
            downloaded.append(str(path))
        if args.json:
            print(json.dumps(downloaded, ensure_ascii=False, indent=2))
        else:
            print(f"Downloaded {len(downloaded)} file(s) to {args.output.resolve()}")
        return 0
    except (MoodleError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
