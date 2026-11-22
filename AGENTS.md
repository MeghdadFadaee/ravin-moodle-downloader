# Repository Guidelines

## Project Structure

Ravin is a Python CLI for organizing authorized Moodle course materials. The package lives in `src/ravin/` and tests live in `tests/`.

- `cli.py` owns argument parsing, shell completion wiring, and command dispatch. `auth.py` manages credentials and browser sessions, `client.py` owns Moodle requests and file transfer, and `parsers.py` interprets Moodle HTML.
- `catalog.py` builds course records from Moodle discovery. `scan.py` reconciles them with local state, `layout.py` repairs activity bundle locations, and `paths.py` handles portable paths and artifact discovery.
- `questions.py`, `assessments.py`, and `recordings.py` manage local additions. `transcribe.py`, `summarize.py`, and `course_pdf.py` produce study artifacts. `exporter.py` and `importer.py` handle portable course ZIPs.
- The static library lives in `public/` and the local byte-range server lives in `server.py`. PDF fonts and their license are packaged in `src/ravin/fonts/`.

## Build, Test, and Development

- `python3 -m pip install -e .` installs the package for local development in a Python 3.10+ virtual environment.
- `python3 -m pip wheel --no-deps . -w dist` builds a wheel with setuptools.
- `ravin --help` checks the CLI entry point. `ravin serve --open` starts the local library and opens it in a browser.
- `python3 -m unittest discover -s tests -v` runs the test suite.
- `python3 -m compileall -q src tests` checks Python syntax, as CI does.
- Use the project virtual environment for these checks. Base CI installs the package with its declared dependencies. Real transcription requires the `transcribe` extra and FFmpeg, real summarization requires an authenticated Codex CLI, and PDF rendering requires the `pdf` extra and Playwright Chromium.

## Coding Style

Use four spaces for Python indentation, `snake_case` for modules and functions, and `PascalCase` for classes. Match nearby type annotations and keep changes compatible with Python 3.10. Keep dependencies to a minimum. CI runs tests and syntax checks without a separate formatter or linter.

## Testing Guidelines

Add or update `unittest` coverage for behavioral changes. Use `tests/test_<feature>.py` files and descriptive `test_*` methods. Use synthetic fixtures and temporary directories for course files. Bash and Zsh are needed to exercise both shell completion paths. CI tests Python 3.10, 3.12, and 3.14 without a numeric coverage threshold.

- Exercise CLI behavior through `ravin.cli.main([...])` when argument handling, output, or exit status changes. Keep progress and diagnostics on stderr when a command emits JSON on stdout.
- Pass a temporary `--public` root and isolate the working directory and environment when testing commands that can migrate a legacy `library/`, save sessions, or write `.ravin/` state. Stub external services and subprocesses. Report live LMS or tool verification separately from local test results.

## Moodle Scans and Downloads

- Keep online discovery separate from file transfer. Identify file responses by their headers and leave their bodies unread during activity inspection. Course downloads go through `ravin download`, which resumes `.part` files, checks byte ranges and available size metadata, and updates the manifests after successful transfers.
- Treat exhausted network retries as scan failures. Preserve the last successful manifests and distinguish connectivity failures from expired sessions before triggering browser authentication. Exercise the scan entry point with synthetic HTTP responses, including the failure path.
- Keep offline scan, artifact generation, and local question and recording imports independent of LMS authentication. An offline scan refreshes local state from existing manifests and preserves the last remote scan timestamp.

## Manifests and Portable Course Data

- Treat `courses/catalog.json` and `courses/<id>/manifest.json` under the public root as a shared contract for the CLI, library UI, and course ZIPs. Keep changes compatible with older manifests. Generate local URLs with the existing path helpers so they remain relative to the public root and work with spaces and Persian filenames.
- Use the existing atomic writers and manifest lock for manifest and layout mutations. Reconcile Moodle bundles by stable activity ID when section or position changes their directory key. Preserve files, partials, artifacts, and archived versions when repairing bundles or migrating the legacy layout.
- Preserve local assessment declarations and questions when Moodle hides or removes a linked quiz. Moodle quizzes should remain visible in the Exams filter even before local attachments or questions exist. Refresh derived manifest state through the existing reconciliation path after local changes.
- Preserve portable artifact metadata and freshness checks across import and sync, including filesystem timestamp rounding. Keep batch dry runs read-only and preserve completed work on interruption. Pass transcript content to Codex as untrusted input through stdin in an isolated workspace.
- Keep ZIP validation and staging before the merge, followed by offline reconciliation. Exports omit videos by default and skip partial files. For changes to SSH sync behavior, check both `ravin sync-public` and `sync-public.sh`, which exclude video files and video partials and enable remote deletion only with `--delete`.

## Release Checks

- For fixes or features shipped to users, follow the [release checklist](CONTRIBUTING.md#release-checklist). Keep the versions in `pyproject.toml` and `src/ravin/constants.py` in sync, add a dated entry to `CHANGELOG.md`, and verify `ravin --version` from the project virtual environment after installation. Documentation-only changes to contributor guidance do not require a release bump.
- Update the README when command usage or user-visible behavior changes. When changing a shared JavaScript or CSS file, update that asset's cache key in both `public/index.html` and `public/course.html`.

## Commits and Pull Requests

Prefer the common history patterns `feat: ...`, `fix: ...`, and scoped forms such as `fix(scan): ...`. Use a short, imperative summary. Describe the problem, approach, and test coverage in the PR. Link related issues and include screenshots for visible library changes when helpful.

## Private Data

Keep `.env`, browser profiles, cookies, credentials, private LMS responses, and downloaded course files out of version control and issue reports. Treat `.ravin/`, `public/courses/`, and `public/exports/` as private generated data. Keep credentials, session tokens, protected download URLs, and machine-specific absolute paths out of generated manifests. Point the static server at the public root and treat its course data and export ZIPs as private when serving or syncing it.
