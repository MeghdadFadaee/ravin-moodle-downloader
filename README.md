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

The tool only requests content that the supplied account is already authorized to view. It does not bypass enrollment, permissions, DRM, or access controls.
