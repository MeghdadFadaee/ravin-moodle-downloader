# Changelog

All notable changes to this project will be documented here.

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
