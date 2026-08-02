# Ravin Moodle Downloader

A dependency-free command-line client that lists your enrolled Moodle courses and downloads accessible course files. It defaults to Ravin Academy, but accepts another Moodle base URL through `--site`.

The client first tries Moodle's official mobile web-service API and falls back to a normal web login plus Moodle's authenticated AJAX interface. For sites protected by Cloudflare, it can reuse request headers from a browser session.

> [!IMPORTANT]
> Use this tool only for courses and files your account is authorized to access. Respect the LMS terms, instructor permissions, and applicable copyright rules. This project does not bypass enrollment, DRM, or access controls.

## Requirements

- Python 3.10 or newer
- An active enrollment on the target Moodle site

The runtime has no third-party Python dependencies.

## Installation

After cloning or downloading the repository, install the command from its root directory:

```bash
python3 -m pip install .
```

You can then use `ravin-downloader` from any shell. Running `python3 ravin_downloader.py` directly from the repository remains supported.

## Quick start

```bash
ravin-downloader courses
ravin-downloader files COURSE_ID
ravin-downloader download COURSE_ID
```

Files are saved under `downloads/<course-id>/...`. Existing completed files are skipped; interrupted downloads use a temporary `.part` suffix. Use `--overwrite` if you want to replace existing files.

For scripting, use JSON output:

```bash
ravin-downloader --json courses
ravin-downloader --json files COURSE_ID
```

## Configuration

On first use, missing values are requested interactively and saved in `.env` in the current directory. Copy [`.env.example`](.env.example) if you prefer to configure it manually. The file is Git-ignored and written with owner-only permissions.

Never commit or share `.env`. It may contain both account credentials and a temporary authenticated browser session.

If the academy disables its mobile token service, the fallback is automatic. You can force that path for troubleshooting:

```bash
ravin-downloader --web-only --username YOUR_USERNAME courses
```

## When Cloudflare blocks terminal login

Ravin Academy currently puts a Cloudflare browser check in front of both Moodle login endpoints. If the normal command reports that Cloudflare requires a real browser session:

1. Open the LMS in a normal browser and log in.
2. Open the browser's Developer Tools, select **Network**, and reload the LMS page.
3. Select the main `view.php` or `courses.php` request.
4. Under **Request Headers**, copy the complete `User-Agent` value and the complete `Cookie` value.
5. Run the browser-session mode:

```bash
ravin-downloader --browser-session courses
```

Paste the User-Agent normally and the Cookie header into the hidden prompt. After the headers are validated, they are saved in `.env` with owner-only permissions and ignored by Git. The same file can contain all four values:

```dotenv
RAVIN_USERNAME="your username"
RAVIN_PASSWORD="your password"
RAVIN_USER_AGENT="the complete browser User-Agent"
RAVIN_COOKIE="the complete browser Cookie header"
```

Values entered at the script's prompts are written or updated automatically. Later commands reuse `.env`, so you can simply run:

```bash
ravin-downloader files 44
ravin-downloader download 44
```

The saved session is checked on every run. When it expires or Cloudflare rejects it, the tool asks for fresh headers and replaces the saved copy. You can force that refresh yourself with:

```bash
ravin-downloader --refresh-session courses
```

Never send `.env`, your password, or its Cookie value to another person; they grant access to your LMS account or signed-in session.

## Development

Run the test suite without installing additional packages:

```bash
python3 -m unittest -v
python3 -m py_compile ravin_downloader.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance and [SECURITY.md](SECURITY.md) for reporting security issues.

## License and disclaimer

Released under the [MIT License](LICENSE). This is an independent community project and is not affiliated with or endorsed by Ravin Academy or Moodle HQ.
