# Ravin Academy course downloader

This small, dependency-free command-line tool lists the Moodle courses available to your account and downloads the files in a selected course. It first tries Moodle's official mobile web-service API and automatically falls back to a normal web login plus Moodle's own authenticated AJAX interface.

Your password is read with a hidden prompt and is never written to disk.

## Usage

Python 3.10 or newer is recommended.

```bash
python3 ravin_downloader.py --username YOUR_USERNAME courses
python3 ravin_downloader.py --username YOUR_USERNAME files 44
python3 ravin_downloader.py --username YOUR_USERNAME download 44
```

Files are saved under `downloads/<course-id>/...`. Existing completed files are skipped; interrupted downloads use a temporary `.part` suffix. Use `--overwrite` if you want to replace existing files.

For scripting, credentials can be supplied for the current shell process and output can be JSON:

```bash
RAVIN_USERNAME='your-user' RAVIN_PASSWORD='your-password' \
  python3 ravin_downloader.py --json courses
```

Avoid putting the password directly in shell history. Prefer the normal hidden password prompt when working interactively.

If the academy disables its mobile token service, the fallback is automatic. You can force that path for troubleshooting:

```bash
python3 ravin_downloader.py --web-only --username YOUR_USERNAME courses
```

## When Cloudflare blocks terminal login

Ravin Academy currently puts a Cloudflare browser check in front of both Moodle login endpoints. If the normal command reports that Cloudflare requires a real browser session:

1. Open the LMS in a normal browser and log in.
2. Open the browser's Developer Tools, select **Network**, and reload the LMS page.
3. Select the main `view.php` or `courses.php` request.
4. Under **Request Headers**, copy the complete `User-Agent` value and the complete `Cookie` value.
5. Run the browser-session mode:

```bash
python3 ravin_downloader.py --browser-session courses
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
python3 ravin_downloader.py files 44
python3 ravin_downloader.py download 44
```

The saved session is checked on every run. When it expires or Cloudflare rejects it, the tool asks for fresh headers and replaces the saved copy. You can force that refresh yourself with:

```bash
python3 ravin_downloader.py --refresh-session courses
```

Never send `.env`, your password, or its Cookie value to another person; they grant access to your LMS account or signed-in session.

The tool only requests content that the supplied account is already authorized to view. It does not bypass enrollment, permissions, DRM, or access controls.
