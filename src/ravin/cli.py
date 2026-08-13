"""Command-line parser and application orchestration."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
from .migration import migrate_library_to_public
from .models import MoodleError
from .questions import format_questions_result, import_questions, questions_wizard
from .scan import format_scan, scan_offline, scan_output, scan_remote, update_download_state
from .server import _serve_library
from .summarize import SummaryOptions, format_summary_result, summarize_courses
from .transcribe import TranscriptionOptions, format_transcription_result, transcribe_courses


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan and download your Ravin Academy Moodle courses.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--site", default=DEFAULT_SITE, help=f"Moodle base URL (default: {DEFAULT_SITE})")
    parser.add_argument(
        "--login-url",
        help=f"account portal used for browser login; defaults to {DEFAULT_RAVIN_LOGIN_URL} for Ravin Academy",
    )
    parser.add_argument("--username", help="LMS username; password is prompted securely")
    parser.add_argument("--web-only", action="store_true", help="skip the Moodle mobile API and use a web session")
    parser.add_argument("--browser-session", action="store_true", help="force browser-session authentication")
    parser.add_argument("--browser-user-agent", help="User-Agent copied from the logged-in browser")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"credentials and browser-session file (default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument("--refresh-session", action="store_true", help="refresh the saved browser session")
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
    parser.add_argument("--browser-executable", type=Path, help="installed browser executable")
    parser.add_argument("--login-timeout", type=int, default=600, help="interactive login timeout")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="open a browser and refresh the saved LMS session")

    scan_parser = subparsers.add_parser("scan", help="scan LMS courses and reconcile local learning state")
    scan_parser.add_argument("course_ids", nargs="*", type=int, help="optional course IDs; defaults to all courses")
    scan_parser.add_argument("--public", type=Path, default=Path("public"), help="public web root")
    scan_parser.add_argument("--offline", action="store_true", help="verify existing manifests without contacting LMS")
    scan_parser.add_argument("--json", action="store_true", help="print machine-readable scan results")

    download_parser = subparsers.add_parser("download", help="scan and download files from one course")
    download_parser.add_argument("course_id", type=int)
    download_parser.add_argument("--public", type=Path, default=Path("public"), help="public web root")
    download_parser.add_argument("--overwrite", action="store_true")
    download_parser.add_argument("--retries", type=int, default=5, help="retry interrupted files (default: 5)")
    download_parser.add_argument("--json", action="store_true", help="print downloaded paths as JSON")

    transcribe_parser = subparsers.add_parser(
        "transcribe",
        help="transcribe downloaded course media with Whisper",
    )
    transcribe_parser.add_argument(
        "course_ids", nargs="*", type=int, help="optional course IDs; defaults to all local courses"
    )
    transcribe_parser.add_argument("--public", type=Path, default=Path("public"), help="public web root")
    transcribe_parser.add_argument("--model", help="Whisper model; defaults to WHISPER_MODEL or large")
    transcribe_parser.add_argument(
        "--device", choices=("auto", "cpu", "cuda", "mps"), help="compute device; defaults to auto"
    )
    transcribe_parser.add_argument(
        "--language", help="Whisper language code; use 'auto' for detection (default: fa)"
    )
    transcribe_parser.add_argument(
        "--retries", type=int, default=2, help="additional attempts per failed file (default: 2)"
    )
    transcribe_parser.add_argument("--overwrite", action="store_true", help="replace matching transcripts")
    transcribe_parser.add_argument("--dry-run", action="store_true", help="show pending media without loading Whisper")
    transcribe_parser.add_argument(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        default=sys.platform == "darwin",
        help="prevent macOS sleep while running (default: enabled on macOS)",
    )
    transcribe_parser.add_argument(
        "--validate-media",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="check for a readable audio stream with ffprobe (default: enabled)",
    )
    transcribe_parser.add_argument("--json", action="store_true", help="print the final result as JSON")

    summarize_parser = subparsers.add_parser(
        "summarize",
        help="generate Persian study summaries from transcripts with Codex CLI",
    )
    summarize_parser.add_argument(
        "course_ids", nargs="*", type=int, help="optional course IDs; defaults to all local courses"
    )
    summarize_parser.add_argument("--public", type=Path, default=Path("public"), help="public web root")
    summarize_parser.add_argument(
        "--model", help="Codex model override; defaults to CODEX_SUMMARY_MODEL or Codex configuration"
    )
    summarize_parser.add_argument(
        "--retries", type=int, default=2, help="additional attempts per failed transcript (default: 2)"
    )
    summarize_parser.add_argument(
        "--timeout", type=int, default=1800, help="maximum seconds for each Codex attempt (default: 1800)"
    )
    summarize_parser.add_argument("--overwrite", action="store_true", help="replace matching summaries")
    summarize_parser.add_argument("--dry-run", action="store_true", help="show pending transcripts without using Codex")
    summarize_parser.add_argument(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        default=sys.platform == "darwin",
        help="prevent macOS sleep while running (default: enabled on macOS)",
    )
    summarize_parser.add_argument("--json", action="store_true", help="print the final result as JSON")

    questions_parser = subparsers.add_parser(
        "questions",
        help="interactively import or update questions for a local quiz",
        description="Import exam questions with a course-and-quiz wizard; all positional values are optional.",
    )
    questions_parser.add_argument("course_id", nargs="?", type=int, help="course containing the quiz")
    questions_parser.add_argument("activity_id", nargs="?", type=int, help="Moodle quiz activity ID")
    questions_parser.add_argument("questions", nargs="?", type=Path, help="UTF-8 Markdown questions and answers")
    questions_parser.add_argument(
        "--file",
        dest="files",
        type=Path,
        action="append",
        default=[],
        help="optional original exam attachment; repeat for multiple files",
    )
    questions_parser.add_argument("--public", type=Path, default=Path("public"), help="public web root")
    questions_parser.add_argument("--json", action="store_true", help="print imported paths as JSON")

    serve_parser = subparsers.add_parser("serve", help="serve the public learning library locally")
    serve_parser.add_argument("--public", type=Path, default=Path("public"), help="public web root")
    serve_parser.add_argument("--host", default="127.0.0.1", help="local bind address")
    serve_parser.add_argument("--port", type=int, default=8765, help="local port")
    serve_parser.add_argument("--open", action="store_true", help="open the library in your default browser")
    return parser


def _maybe_migrate(public: Path) -> None:
    if any((public / "courses").glob("*/manifest.json")):
        return
    legacy = Path("library").resolve()
    if (legacy / "courses.json").is_file():
        print("Migrating the previous library layout...", file=sys.stderr)
        migrate_library_to_public(legacy, public)


def _authenticate(args: argparse.Namespace) -> tuple[MoodleClient, str]:
    args.env_values = _load_env_file(args.env_file)
    saved_session = _browser_session_values(args, fresh=args.refresh_session)
    if args.browser_session or args.refresh_session or saved_session is not None:
        return _authenticate_browser_session(args, saved_session)
    username, password = _credentials(args)
    _save_env_values(
        args.env_file,
        {ENV_KEYS["username"]: username, ENV_KEYS["password"]: password},
    )
    args.env_values.update({ENV_KEYS["username"]: username, ENV_KEYS["password"]: password})
    client = MoodleClient(args.site, username, password, web_only=args.web_only)
    try:
        mode = client.authenticate()
    except MoodleError as exc:
        if "cloudflare requires a real browser session" not in str(exc).casefold():
            raise
        print("Cloudflare blocked password login; opening the authentication browser.", file=sys.stderr)
        return _authenticate_browser_session(args)
    return client, mode


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            _serve_library(args.public, args.host, args.port, args.open)
            return 0

        public = getattr(args, "public", Path("public")).expanduser().resolve()
        if args.command in {"scan", "download", "transcribe", "summarize", "questions"}:
            _maybe_migrate(public)

        if args.command == "scan" and args.offline:
            catalog = scan_offline(public, args.course_ids)
            print(json.dumps(scan_output(catalog), ensure_ascii=False, indent=2) if args.json else format_scan(catalog))
            return 0

        if args.command == "transcribe":
            env_values = _load_env_file(args.env_file)
            model = args.model or env_values.get("WHISPER_MODEL") or os.getenv("WHISPER_MODEL") or "large"
            device = args.device or env_values.get("WHISPER_DEVICE") or os.getenv("WHISPER_DEVICE") or "auto"
            language_value = (
                args.language
                if args.language is not None
                else env_values.get("WHISPER_LANGUAGE", os.getenv("WHISPER_LANGUAGE", "fa"))
            )
            language = None if not language_value or language_value.casefold() == "auto" else language_value
            result = transcribe_courses(
                TranscriptionOptions(
                    public=public,
                    course_ids=tuple(args.course_ids),
                    model=model,
                    device=device,
                    language=language,
                    retries=args.retries,
                    overwrite=args.overwrite,
                    dry_run=args.dry_run,
                    keep_awake=args.keep_awake,
                    validate_media=args.validate_media,
                )
            )
            print(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
                if args.json else format_transcription_result(result)
            )
            return result.exit_code

        if args.command == "summarize":
            env_values = _load_env_file(args.env_file)
            model = args.model or env_values.get("CODEX_SUMMARY_MODEL") or os.getenv("CODEX_SUMMARY_MODEL")
            result = summarize_courses(
                SummaryOptions(
                    public=public,
                    course_ids=tuple(args.course_ids),
                    model=model,
                    retries=args.retries,
                    timeout=args.timeout,
                    overwrite=args.overwrite,
                    dry_run=args.dry_run,
                    keep_awake=args.keep_awake,
                )
            )
            print(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
                if args.json else format_summary_result(result)
            )
            return result.exit_code

        if args.command == "questions":
            interactive = args.course_id is None or args.activity_id is None or args.questions is None
            course_id, activity_id, questions_path, attachment_paths = questions_wizard(
                public,
                args.course_id,
                args.activity_id,
                args.questions,
                tuple(args.files),
                prompt_for_attachment=interactive,
            ) if interactive else (
                args.course_id,
                args.activity_id,
                args.questions,
                tuple(args.files),
            )
            result = import_questions(
                public,
                course_id,
                activity_id,
                questions_path,
                attachment_paths,
            )
            print(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
                if args.json else format_questions_result(result)
            )
            return 0

        args.env_values = _load_env_file(args.env_file)
        if args.command == "login":
            _client, mode = _authenticate_browser_session(args)
            print(f"Authenticated using {mode}; updated {args.env_file}.")
            return 0

        client, mode = _authenticate(args)
        print(f"Authenticated using {mode}.", file=sys.stderr)

        if args.command == "scan":
            catalog = scan_remote(client, public, args.course_ids)
            print(json.dumps(scan_output(catalog), ensure_ascii=False, indent=2) if args.json else format_scan(catalog))
            return 0

        scan_remote(client, public, [args.course_id])
        files = client.list_files(args.course_id)
        if not files:
            print("No downloadable files were found.")
            return 0
        downloaded: list[str] = []
        for number, item in enumerate(files, 1):
            print(f"[{number}/{len(files)}] {item.activity}: {item.filename}", file=sys.stderr)
            path = client.download(
                item,
                public,
                overwrite=args.overwrite,
                retries=max(args.retries, 0),
            )
            update_download_state(public, item, path)
            downloaded.append(str(path))
        scan_offline(public, [args.course_id])
        if args.json:
            print(json.dumps(downloaded, ensure_ascii=False, indent=2))
        else:
            print(f"Downloaded {len(downloaded)} file(s) into {public / 'courses' / str(args.course_id)}")
        return 0
    except (MoodleError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
