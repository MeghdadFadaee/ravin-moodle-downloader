# Ravin Moodle Downloader

A dependency-free command-line client that lists your enrolled Moodle courses and downloads accessible course files. It defaults to Ravin Academy, but accepts another Moodle base URL through `--site`.

The client first tries Moodle's official mobile web-service API and falls back to a normal web login plus Moodle's authenticated AJAX interface. For sites protected by Cloudflare, it can establish and refresh an authenticated browser session automatically.

> [!IMPORTANT]
> Use this tool only for courses and files your account is authorized to access. Respect the LMS terms, instructor permissions, and applicable copyright rules. This project does not bypass enrollment, DRM, or access controls.

## Requirements

- Python 3.10 or newer
- An active enrollment on the target Moodle site

The downloader itself uses only Python's standard library. Automatic browser login uses the optional Selenium package with an installed Zen, Firefox, Chrome, Chromium, or Brave browser.

## Installation

After cloning or downloading the repository, install the command from its root directory:

```bash
python3 -m pip install '.[browser]'
```

You can then use `ravin-downloader` from any shell. Running `python3 ravin_downloader.py` directly from the repository remains supported.

## Quick start

```bash
ravin-downloader login
ravin-downloader courses
ravin-downloader files COURSE_ID
ravin-downloader download COURSE_ID
ravin-downloader library
ravin-downloader serve-library --open
```

Files are saved under `downloads/<course-id>/...`. Existing completed files are skipped; interrupted downloads use a temporary `.part` suffix and resume automatically with HTTP byte-range requests. Use `--overwrite` if you want to replace existing files, or `--retries` to change the default five retries per file.

For scripting, use JSON output:

```bash
ravin-downloader --json courses
ravin-downloader --json files COURSE_ID
```

## Configuration

On first use, missing values are requested interactively and saved in `.env` in the current directory. Copy [`.env.example`](.env.example) if you prefer to configure it manually. The file is Git-ignored and written with owner-only permissions.

`ravin-downloader login` opens an installed browser using the Git-ignored `.ravin-browser-profile/` directory. For Ravin Academy it starts at `https://lms.ravinacademy.com/`, signs in there, follows the available Moodle course-launch link, and captures the resulting session on `training.ravinacademy.com`. If `.env` already contains `RAVIN_USERNAME` and `RAVIN_PASSWORD`, a compatible login form is filled automatically. Once login succeeds, the browser closes and `.env` is updated with the exact User-Agent and cookies. Use `--browser-executable /path/to/browser` to override automatic detection.

Never commit or share `.env`. It may contain both account credentials and a temporary authenticated browser session.

If the academy disables its mobile token service, the fallback is automatic. You can force that path for troubleshooting:

```bash
ravin-downloader --web-only --username YOUR_USERNAME courses
```

## Browser login and Cloudflare

Ravin Academy protects its account portal and Moodle with a browser check. Set up the browser helper once:

```bash
python3 -m pip install '.[browser]'
ravin-downloader login
```

After authentication, `.env` can contain these values:

```dotenv
RAVIN_USERNAME="your username"
RAVIN_PASSWORD="your password"
RAVIN_LOGIN_URL="https://lms.ravinacademy.com/"
RAVIN_USER_AGENT="the complete browser User-Agent"
RAVIN_COOKIE="the complete browser Cookie header"
```

Later commands reuse `.env`, so you can simply run:

```bash
ravin-downloader files 44
ravin-downloader download 44
```

## Static course library

Build a clean local website from your enrolled courses and downloaded files:

```bash
ravin-downloader library
ravin-downloader serve-library --open
```

The first command refreshes `library/courses.json` from the LMS and prepares `library/` as a self-contained web root. The catalog preserves the LMS chapter order and includes section IDs and summaries, every activity and its type, lesson descriptions, LMS completion state, original filenames, MIME types, sizes, extensions, local download status, and safe LMS activity links. Large course files are not copied: `library/media` is a relative symlink that points only to `downloads/`.

The second command serves only the prepared web root at `http://localhost:8765/`. The library includes course search, true chapter grouping, offline progress, resource filters, efficient seekable video playback, document links, online LMS activities, dark mode, and completion checkboxes saved in your browser. Run `ravin-downloader library` again after downloading new files to refresh the catalog.

To build only selected courses or choose other directories:

```bash
ravin-downloader library 44 --output library --downloads downloads
ravin-downloader serve-library --site-dir library --downloads downloads --port 8765
```

If the catalog data is already current and you only need to rebuild the pages, symlink, or Nginx configuration, no LMS connection is required:

```bash
ravin-downloader library --reuse-catalog
```

The generated directory contains:

```text
library/
├── index.html
├── course.html
├── app.js
├── styles.css
├── courses.json
├── nginx-server.conf
└── media -> ../downloads
```

For Nginx, include the generated server block inside the `http {}` section of your main configuration:

```nginx
include /absolute/path/to/your/clone/library/nginx-server.conf;
```

Then check and reload Nginx:

```bash
nginx -t
brew services restart nginx
```

Open `http://localhost:8765/`. Nginx now uses `library/` as its complete document root. The repository, `.env`, browser profile, source code, and Git metadata are outside that root and cannot be requested through the site. Only the explicit `media` symlink exposes `downloads/`.

The generated `library/` directory is Git-ignored because its JSON contains details about your enrollments. No passwords, cookies, session tokens, or remote protected-file URLs are written to the catalog.

The saved session is checked on every run. When it expires or Cloudflare rejects it, the authentication browser opens automatically and replaces the saved values. You can force that refresh yourself with:

```bash
ravin-downloader --refresh-session courses
```

If browser automation is unavailable, manual header entry remains available as a fallback:

```bash
ravin-downloader --manual-session --refresh-session courses
```

Never send `.env`, `.ravin-browser-profile/`, your password, or its Cookie value to another person; they grant access to your LMS account or signed-in session.

## Development

Run the test suite without installing additional packages:

```bash
python3 -m unittest -v
python3 -m py_compile ravin_downloader.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance and [SECURITY.md](SECURITY.md) for reporting security issues.

## License and disclaimer

Released under the [MIT License](LICENSE). This is an independent community project and is not affiliated with or endorsed by Ravin Academy or Moodle HQ.
