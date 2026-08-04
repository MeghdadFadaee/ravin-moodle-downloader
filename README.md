# Ravin Moodle Downloader

A small command-line client for scanning authorized Moodle courses, downloading and transcribing their files, and browsing them in a private static learning library. It defaults to Ravin Academy and can target another Moodle base URL with `--site`.

The client tries Moodle's mobile API first, falls back to its authenticated web interface, and can establish a real browser session for sites protected by Cloudflare.

> [!IMPORTANT]
> Use this tool only for courses and files your account may access. Respect LMS terms, instructor permissions, and copyright. The project does not bypass enrollment, DRM, or access controls.

## Install

Requirements:

- Python 3.10 or newer
- An active enrollment on the target Moodle site
- Zen, Firefox, Chrome, Chromium, or Brave for automatic browser login
- FFmpeg when using local Whisper transcription

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

An activity can also contain generated study material:

```text
artifacts/
├── transcript.fa.txt
├── transcript.meta.json
├── summary.fa.md
├── summary.meta.json
└── questions.fa.md
```

The scanner reports each applicable file, transcript, summary, and assessment as `missing`, `partial`, `complete`, `stale`, or `error`. Non-applicable states are recorded as `not_applicable`. Transcript freshness is checked against its source file metadata; summary freshness is checked against the transcript hash.

The downloader updates the course manifest after every completed file. A final offline scan reconciles all totals, so an interrupted run still retains useful progress.

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

## Static learning library

The web interface is already present in the tracked `public/` directory. Generate or refresh its private data, then serve it:

```bash
ravin scan
ravin serve --open
```

The site provides course search, chapter grouping, local availability and artifact states, video playback, document and assessment links, transcript and summary readers, Markdown question rendering, dark mode, and browser-local completion checkboxes.

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
