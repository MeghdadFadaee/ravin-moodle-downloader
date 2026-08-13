# Changelog

All notable changes to this project will be documented here.

## 0.14.0 - 2026-08-13

- Add `ravin import URL` for restoring backups and updating course mirrors from direct export links.
- Validate ZIP structure, paths, links, duplicate entries, manifests, and file integrity before merging.
- Preserve local-only files while giving archive files priority at matching paths.
- Replace imported files atomically and automatically reconcile the complete library with an offline scan.
- Add `--timeout`, `--public`, and machine-readable `--json` import options.

## 0.13.0 - 2026-08-13

- Add a local `ravin export` command that creates timestamped archives under `public/exports/` from all scanned courses.
- Exclude current and archived videos by default while retaining other course files and learning artifacts.
- Add `--include-videos`, `--output`, `--public`, and machine-readable `--json` options.
- Build ZIP files atomically with ZIP64 support and skip partial downloads, locks, and platform metadata.
- Keep export destinations outside the course data directory and ignore generated public exports in Git.

## 0.12.0 - 2026-08-13

- Add per-category missing, partial, stale, and error details to scan output.
- Print ordered next-step commands for downloading, transcribing, summarizing, and importing exam questions.
- Report missing live-class recordings as optional follow-up work.
- Exclude empty LMS resource activities from downloadable-file totals.
- Recognize files written by the previous mojibake filename bug without renaming or modifying course content.
- Repair future UTF-8 `Content-Disposition` filenames and avoid redownloading known legacy paths.
- Replace the misleading download total with a checked-entry count followed by reconciled course status.

## 0.11.0 - 2026-08-13

[- Add a `ravin recording` wizard for attaching local videos to Moodle live-class activities.
- Make imported recordings available to the existing Whisper transcription and Codex summarization pipelines.
- Preserve replaced recordings as archived versions and expose current and archived files in the library.
- Keep the selected current recording across later online LMS scans with portable local metadata.
- Accept pasted interactive file paths containing spaces without requiring shell quoting.
]()
## 0.10.0 - 2026-08-13

- Track the Moodle-selected resource as current while retaining other local files as archived versions.
- Preserve an existing same-name download under `files/archive/` before atomically installing its replacement.
- Show current and archived files in the static library, including playback for archived videos.
- Mark transcripts and summaries stale when they were generated from an archived media version.
- Match scanner paths using the downloader's filename normalization so Moodle whitespace changes do not create false missing-file reports.
- Apply the same filename normalization during transcription discovery when archived media is also present.

## 0.9.0 - 2026-08-13

- Add an interactive `ravin questions` wizard for importing or updating Markdown exam questions.
- List local courses and their quiz activities, including current question status, for guided selection.
- Resolve the ordered activity bundle from the course manifest instead of requiring manual directory naming.
- Support repeatable `--file` options for original PDFs and other exam attachments.
- Validate quiz activities and UTF-8 Markdown, write files atomically, and refresh manifests without LMS login.

## 0.8.0 - 2026-08-04

- Add a manifest-driven `ravin summarize` command that generates Persian Markdown study guides with Codex CLI.
- Run Codex non-interactively in an ephemeral, read-only sandbox and capture only its final response.
- Skip summaries whose transcript hash and metadata still match, with an explicit overwrite option.
- Retry individual failures, continue with later lessons, preserve interruption state, and keep macOS awake.
- Update course manifests after every result and expose failed summary state without machine-specific paths.
- Add configurable Codex model and per-attempt timeout options.

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
