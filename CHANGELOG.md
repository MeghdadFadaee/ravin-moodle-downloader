# Changelog

All notable changes to this project will be documented here.

## 0.7.0 - 2026-08-04

- Add a manifest-driven `ravin transcribe` command using OpenAI Whisper.
- Discover downloaded video and audio in LMS order and update course manifests after every result.
- Resume safely by matching source metadata, Whisper model, and language before skipping completed transcripts.
- Validate media with FFprobe, retry individual failures, continue with later lessons, and persist private run state and error logs.
- Record failed transcript state in course manifests without exposing machine-specific paths.
- Keep macOS awake during long runs and preserve atomic transcript and metadata writes across interruption.
- Add the optional `transcribe` dependency group for the large Whisper and PyTorch runtime.

## 0.6.0 - 2026-08-04

- Replace the separate course, file, and library builders with one `ravin scan` command.
- Add per-course manifests and a small global catalog under the Git-ignored `public/courses/` directory.
- Reconcile download, transcript, summary, and assessment states, including stale artifact detection.
- Update manifests after every completed download and add an authentication-free offline scan.
- Move the shared static interface into the tracked `public/` web root while keeping all personal course data ignored.
- Load course details from their own manifests and show learning-artifact states in the library.
- Remove redundant per-activity `item.json` files and migrate the previous generated library layout automatically.

## 0.5.0 - 2026-08-04

- Replace the `ravin-downloader` console command with the shorter `ravin` command.
- Make browser automation part of the default installation, so `python3 -m pip install .` is sufficient.
- Move the project into a standard `src/ravin/` package layout.
- Split the former monolithic module into dedicated CLI, authentication, client, parser, path, catalog, migration, server, and model modules.
- Move static library assets into the package and move tests under `tests/`.
- Remove the old root module and its legacy installed package files.

## 0.4.0 - 2026-08-04

- Store downloads directly in ordered library activity bundles named `SECTION_NUMBER--ACTIVITY_POSITION--ACTIVITY_ID`.
- Preserve original filenames while keeping LMS titles and activity metadata in portable `item.json` files.
- Add an offline `migrate-library` command for the previous flat download layout.
- Normalize transcript metadata and summary references so they contain no machine-specific absolute paths.
- Place local exams in their actual LMS position and expose them as assessment records.
- Add built-in transcript, summary, and Markdown exam-question readers to the static library.
- Remove the download-directory media symlink; the library is now the only served content root.

## 0.3.0 - 2026-08-02

- Add a generated static Learning Library with responsive course and resource pages.
- Export rich course, chapter, activity, file, completion, and local download metadata to `library/courses.json`.
- Add course search, file filters, video playback, dark mode, and local completion tracking.
- Preserve Moodle chapter ordering and include forums, quizzes, live classes, and course links.
- Stream local media with HTTP byte-range support for efficient seeking.
- Make `library/` a standalone safe web root with a `media` download symlink and generated Nginx server block.
- Add offline catalog reuse for rebuilding the static/Nginx layout without contacting the LMS.
- Add `library` and `serve-library` commands while keeping generated enrollment data private.

## 0.2.0 - 2026-08-02

- Add a persistent, interactive installed-browser login flow that handles Cloudflare normally.
- Start Ravin authentication at its account portal and automatically follow its Moodle handoff.
- Capture the launched browser's User-Agent and cookies and update `.env` automatically.
- Automatically reopen the authentication browser when a saved session expires.
- Keep manual request-header entry as an explicit fallback.

## 0.1.0 - 2026-08-02

- List enrolled Moodle courses through the mobile API or authenticated AJAX fallback.
- List and download accessible course resources with safe partial-file handling.
- Resume interrupted files with HTTP byte ranges and automatic retry backoff.
- Support Cloudflare-protected sites through a user-provided browser session.
- Load and securely update credentials and browser-session values in a Git-ignored `.env` file.
- Provide JSON output for scripting.
