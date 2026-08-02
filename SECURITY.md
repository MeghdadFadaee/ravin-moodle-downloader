# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Security advisories → Report a vulnerability** feature when it is available for this repository. Do not open a public issue containing exploit details, credentials, cookies, private course URLs, or personal information.

Include the affected version, a minimal reproduction using synthetic data, and the expected impact. Remove all real LMS secrets before attaching logs.

## Credential handling

The downloader may store `RAVIN_USERNAME`, `RAVIN_PASSWORD`, `RAVIN_USER_AGENT`, and `RAVIN_COOKIE` in a local `.env` file. That file is excluded from Git and created with owner-only permissions, but users are still responsible for protecting it. Browser cookies should be treated like passwords and refreshed if exposed.

Only download content your account is authorized to access. This project does not support bypassing authentication, enrollment, DRM, or other access controls.
