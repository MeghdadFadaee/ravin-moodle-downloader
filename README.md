# Ravin Moodle Downloader

A small command-line client for scanning authorized Moodle courses, downloading, transcribing, and summarizing their files, and browsing them in a private static learning library. It defaults to Ravin Academy and can target another Moodle base URL with `--site`.

The client tries Moodle's mobile API first, falls back to its authenticated web interface, and can establish a real browser session for sites protected by Cloudflare.

> [!IMPORTANT]
> Use this tool only for courses and files your account may access. Respect LMS terms, instructor permissions, and copyright. The project does not bypass enrollment, DRM, or access controls.

## Install

Requirements:

- Python 3.10 or newer
- An active enrollment on the target Moodle site
- Zen, Firefox, Chrome, Chromium, or Brave for automatic browser login
- FFmpeg when using local Whisper transcription
- A logged-in Codex CLI installation when generating summaries

From the repository root:

```bash
python3 -m pip install .
```

This installs the `ravin` command and its browser automation dependency. The equivalent module entry point is `python3 -m ravin`.

Whisper and PyTorch are large, platform-specific dependencies, so install them only on the machine that will transcribe media:

```bash
brew install ffmpeg
python3 -m pip install '.[transcribe]'
```

## Quick start

```bash
ravin login
ravin scan
ravin download COURSE_ID
ravin transcribe COURSE_ID
ravin summarize COURSE_ID
ravin questions
ravin recording
ravin serve --open
```

`scan` replaces the former separate course-listing, file-listing, and library-generation commands. It reads the LMS structure, reconciles everything already present on disk, and writes the JSON manifests consumed by the static site.

Scan only selected courses or print machine-readable results:

```bash
ravin scan 44 45
ravin scan 44 --json
```

Check downloads and learning artifacts without logging in or contacting the LMS:

```bash
ravin scan --offline
```

## Authentication and `.env`

`ravin login` opens a persistent, Git-ignored browser profile. For Ravin Academy it begins at `https://lms.ravinacademy.com/`, follows the Moodle launch into `training.ravinacademy.com`, and stores the resulting User-Agent and cookies in `.env`.

Missing credentials are requested interactively. You can also copy [`.env.example`](.env.example) and fill it yourself:

```dotenv
RAVIN_USERNAME="your username"
RAVIN_PASSWORD="your password"
RAVIN_LOGIN_URL="https://lms.ravinacademy.com/"
RAVIN_USER_AGENT="the complete browser User-Agent"
RAVIN_COOKIE="the complete browser Cookie header"
```

`.env` is Git-ignored and written with owner-only permissions. Never commit or share it, the browser profile, your password, or your Cookie value.

Saved sessions are checked before use. If one expires or Cloudflare rejects it, Ravin opens the authentication browser and replaces the saved values. Useful alternatives are:

```bash
ravin --refresh-session scan
ravin --manual-session --refresh-session scan
ravin --web-only --username YOUR_USERNAME scan
```

## Downloads and activity bundles

Download a course with:

```bash
ravin download 44
```

Files are stored directly under `public/courses/<course-id>/content/`. Each LMS activity has one sortable, title-independent bundle named:

```text
SECTION_NUMBER--ACTIVITY_POSITION--ACTIVITY_ID
```

Original filenames stay unchanged in the bundle's `files/` directory. Interrupted downloads use a `.part` suffix and resume with HTTP byte ranges. Completed files are skipped unless `--overwrite` is supplied.

When an instructor replaces a resource, the manifest treats the filename currently published by Moodle as the **current** version and keeps other local files as **archived** versions. A replacement with the same filename and a different known size is downloaded automatically; `--overwrite` can force a same-name refresh when Moodle does not report a size. In both cases the existing file is preserved under `files/archive/` before the new download takes its place.

The library's main Play or Open action always uses the current file. Older files remain available in an expandable archived-version list. If a transcript was generated from an archived file, both it and its dependent summary are marked stale; run `ravin transcribe COURSE_ID` and then `ravin summarize COURSE_ID` to regenerate them for the current file.

An activity can also contain generated study material:

```text
artifacts/
├── transcript.fa.txt
├── transcript.meta.json
├── summary.fa.md
├── summary.meta.json
└── questions.fa.md
```

The scanner reports each applicable file, transcript, summary, and assessment as `missing`, `partial`, `complete`, `stale`, or `error`. Each course includes an ordered **Next** section with copyable commands for downloads, transcription, summaries, and exam questions; missing live-class recordings are shown separately as optional work. Non-applicable states are recorded as `not_applicable`. Transcript freshness is checked against its source file metadata; summary freshness is checked against the transcript hash.

Empty LMS resource activities that expose no actual file are not counted as downloads. Older files saved with broken UTF-8 header names are recognized in place without renaming or altering their contents. After `ravin download`, the command says how many LMS entries it checked and prints the same reconciled status and next actions instead of claiming every existing file was newly downloaded.

The downloader updates the course manifest after every completed file. A final offline scan reconciles all totals, so an interrupted run still retains useful progress.

## Exam questions and answers

Start the interactive import wizard:

```bash
ravin questions
```

The wizard:

1. Lists local courses that contain exams.
2. Lists the selected course's exams and their current question status.
3. Prompts for the UTF-8 Markdown questions-and-answers file.
4. Prompts for an optional original exam PDF.

You can enter either a displayed list number or the actual course/activity ID. File paths can be pasted or dragged into the terminal.

For scripting, all values can still be passed directly:

```bash
ravin questions 44 5679 ~/Downloads/questions.fa.md \
  --file ~/Downloads/5679.pdf
```

The command finds the quiz in the course manifest and creates the correct ordered bundle automatically. For the example above, it writes:

```text
public/courses/44/content/026--001--5679/
├── files/5679.pdf
└── artifacts/questions.fa.md
```

Use `--file` more than once when an exam has multiple attachments. The attachment is optional when only updating questions or answers:

```bash
ravin questions 44 5679 revised-questions.fa.md
```

The import is local and requires no LMS login. Files are written atomically, the original attachment names are preserved, and the course manifest is refreshed immediately so the library exposes the exam under its **Questions** button.

## Live-class recordings

Add a video you recorded or received for a Moodle live-class activity with:

```bash
ravin recording
```

The wizard lists courses with live classes, lets you select the class, and prompts for the local video. You can paste or drag a path into the terminal, including an unquoted path containing spaces. For scripting, provide all values directly:

```bash
ravin recording 44 5097 ~/Downloads/group-a-class.mp4
```

The video is copied into the live class's ordered activity bundle and appears in the library as its current playable file. Re-importing the same filename preserves the previous copy under `files/archive/`; importing a differently named recording keeps the previous file as an archived version. The selected current recording survives later online scans.

Imported recordings use the normal media pipeline:

```bash
ravin transcribe 44
ravin summarize 44
```

If a recording is replaced, any transcript and summary generated from the older version are marked stale and will be regenerated by those commands.

## Whisper transcription

After downloading a course, transcribe its video and audio in LMS order:

```bash
ravin transcribe 44
```

The command reads local course manifests and does not contact or log in to the LMS. With no course IDs it processes all local courses:

```bash
ravin transcribe
```

Each successful lesson produces `artifacts/transcript.fa.txt` and portable `artifacts/transcript.meta.json`. The course manifest and global catalog are reconciled after every file, so the transcript becomes available in the library immediately.

Long runs are designed to resume safely:

- Existing output is skipped only when its source size and modification time, Whisper model, and language still match.
- FFprobe validates the audio stream before Whisper starts.
- Each failed validation or transcription is retried twice by default, for three total attempts.
- A permanently failed lesson is recorded and the next lesson still runs.
- Transcript and metadata writes are atomic, so interruption cannot leave a completed-looking partial file.
- macOS sleep is prevented by default while transcription is active.
- `SIGINT` and `SIGTERM` preserve completed work and write an interrupted state for the next run.

Private run state, the final summary, and the append-only error log live under `.ravin/transcribe/`, outside the served `public/` directory. Rerun the same command to skip completed work and retry failures.

Useful options:

```bash
# Preview pending lessons without loading Whisper.
ravin transcribe 44 --dry-run

# Use a smaller model or automatic language detection.
ravin transcribe 44 --model small --language auto

# Replace even matching transcripts.
ravin transcribe 44 --overwrite

# Change the retry count or allow macOS to sleep.
ravin transcribe 44 --retries 4 --no-keep-awake
```

The defaults can also be stored in `.env`:

```dotenv
WHISPER_MODEL="large"
WHISPER_DEVICE="auto"
WHISPER_LANGUAGE="fa"
```

The `large` model needs substantial memory. Use `small` or `turbo` if the machine cannot load it. The first run may download model weights.

## Codex summaries

Generate Persian Markdown study guides from completed transcripts:

```bash
codex login
ravin summarize 44 --dry-run
ravin summarize 44
```

The command uses `codex exec` non-interactively with an ephemeral session, an empty temporary workspace, and a read-only sandbox. Transcript text is passed only through standard input as untrusted content, and Codex is instructed to return only a faithful Persian Markdown study guide without external research. The LMS is never contacted, and `.env` is not placed in the Codex workspace.

Each successful lesson produces `artifacts/summary.fa.md` and portable `artifacts/summary.meta.json`. Existing summaries are skipped when their transcript hash, size, and modification time still match. Use `--overwrite` to regenerate them.

Like transcription, summarization is safe to leave running:

- Each failed Codex invocation is retried twice by default, then the batch continues.
- Results and metadata are written atomically.
- Course manifests are refreshed after every success or failure.
- `SIGINT` and `SIGTERM` preserve completed work for the next run.
- macOS sleep is prevented during active batches by default.
- Missing transcripts are reported as unavailable and do not stop other lessons.

Private state and error logs live under `.ravin/summarize/`. Useful options include:

```bash
# Override the model selected by your Codex configuration.
ravin summarize 44 --model MODEL

# Regenerate fresh summaries, allow more retries, or change the per-attempt timeout.
ravin summarize 44 --overwrite --retries 4 --timeout 3600

# Allow macOS to sleep.
ravin summarize 44 --no-keep-awake
```

Set `CODEX_SUMMARY_MODEL` in `.env` to make a model override persistent. Leave it empty to use the model configured by Codex CLI.

## Static learning library

The web interface is already present in the tracked `public/` directory. Generate or refresh its private data, then serve it:

```bash
ravin scan
ravin serve --open
```

The site provides course search, chapter grouping, local availability and artifact states, current and archived file playback, document and assessment links, transcript and summary readers, Markdown question rendering, dark mode, and browser-local completion checkboxes.

The layout is:

```text
public/
├── index.html                 # tracked UI
├── course.html                # tracked UI
├── assets/                    # tracked CSS and JavaScript
└── courses/                   # Git-ignored private/generated data
    ├── catalog.json
    └── COURSE_ID/
        ├── manifest.json
        └── content/
            └── SECTION--POSITION--ACTIVITY/
                ├── files/
                └── artifacts/
```

Only `public/courses/` is ignored: contributors can safely commit changes to the shared HTML, CSS, and JavaScript without publishing their enrollments or downloaded material. Manifests never contain passwords, cookies, session tokens, protected file URLs, or machine-specific absolute paths.

### Nginx

Point Nginx at `public/`, not the repository root. Replace the example root with the absolute path to your clone:

```nginx
server {
    listen 8765;
    server_name localhost;

    root /absolute/path/to/easy-learn/public;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }
}
```

After checking and reloading Nginx, open `http://localhost:8765/`. Because its document root is only `public/`, `.env`, source code, the browser profile, and Git metadata cannot be requested from the web server. Nginx handles byte-range media playback automatically.

### Previous layouts

When an older generated `library/courses.json` exists and `public/` has no manifests yet, the first `ravin scan` or `ravin download` performs a one-time migration. It moves the existing activity bundles into `public/courses/`, creates manifests, removes obsolete per-activity `item.json` files, and leaves downloaded media intact.

## Command reference

```text
ravin login
ravin scan [COURSE_ID ...] [--offline] [--json] [--public PATH]
ravin download COURSE_ID [--overwrite] [--retries N] [--json] [--public PATH]
ravin transcribe [COURSE_ID ...] [--model MODEL] [--device DEVICE] [--language LANGUAGE]
ravin summarize [COURSE_ID ...] [--model MODEL] [--retries N] [--timeout SECONDS]
ravin questions [COURSE_ID] [ACTIVITY_ID] [QUESTIONS.md] [--file ATTACHMENT ...]
ravin recording [COURSE_ID] [ACTIVITY_ID] [VIDEO]
ravin serve [--host ADDRESS] [--port PORT] [--open] [--public PATH]
```

Authentication options such as `--site`, `--username`, `--refresh-session`, and `--manual-session` go before the command. Run `ravin --help` or `ravin COMMAND --help` for the complete reference.

## Development

The project uses a standard `src/` package layout:

```text
src/ravin/
├── cli.py          # commands and orchestration
├── auth.py         # environment and browser authentication
├── client.py       # Moodle API/web client and downloads
├── parsers.py      # Moodle HTML parsers
├── paths.py        # portable content paths and artifacts
├── catalog.py      # remote course catalog construction
├── scan.py         # manifests and local state reconciliation
├── transcribe.py   # resilient manifest-driven Whisper batches
├── summarize.py    # resilient Codex CLI study-guide batches
├── questions.py    # local exam-question and attachment imports
├── recordings.py   # local live-class recording imports
├── local_files.py  # atomic local copies and archived versions
├── wizard.py       # shared interactive import helpers
├── migration.py    # previous-layout migration
├── server.py       # local range-aware HTTP server
└── models.py       # shared value objects and errors
```

Run the checks with:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) for project guidance.

## License and disclaimer

Released under the [MIT License](LICENSE). This independent community project is not affiliated with or endorsed by Ravin Academy or Moodle HQ.
