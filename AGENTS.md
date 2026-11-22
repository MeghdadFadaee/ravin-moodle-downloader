# Repository Guidelines

## Project Structure

Ravin is a Python CLI for organizing authorized Moodle course materials. The package lives in `src/ravin/`. Add command wiring in `cli.py`, Moodle access in `auth.py` and `client.py`, and manifest reconciliation in `scan.py` and `layout.py`. The static learning library lives in `public/`, with JavaScript and CSS in `public/assets/`. PDF fonts are packaged in `src/ravin/fonts/`. Unit tests live in `tests/`.

## Build, Test, and Development

- `python3 -m pip install -e .` installs the package for local development in a Python 3.10+ virtual environment.
- `python3 -m pip wheel --no-deps . -w dist` builds a wheel with setuptools.
- `ravin --help` checks the CLI entry point. `ravin serve --open` starts the local library and opens it in a browser.
- `python3 -m unittest discover -s tests -v` runs the test suite.
- `python3 -m compileall -q src tests` checks Python syntax, as CI does.

## Coding Style

Use four spaces for Python indentation, `snake_case` for modules and functions, and `PascalCase` for classes. Match nearby type annotations and keep changes compatible with Python 3.10. Keep dependencies to a minimum. CI runs tests and syntax checks without a separate formatter or linter.

## Testing Guidelines

Add or update `unittest` coverage for behavioral changes. Use `tests/test_<feature>.py` files and descriptive `test_*` methods. Use synthetic fixtures and temporary directories for course files. Bash and Zsh are needed to exercise both shell completion paths. CI tests Python 3.10, 3.12, and 3.14 without a numeric coverage threshold.

## Commits and Pull Requests

Prefer the common history patterns `feat: ...`, `fix: ...`, and scoped forms such as `fix(scan): ...`. Use a short, imperative summary. Describe the problem, approach, and test coverage in the PR. Link related issues and include screenshots for visible library changes when helpful.

## Private Data

Keep `.env`, browser profiles, cookies, credentials, private LMS responses, and downloaded course files out of version control and issue reports. Treat `public/courses/` and `public/exports/` as private generated data.
