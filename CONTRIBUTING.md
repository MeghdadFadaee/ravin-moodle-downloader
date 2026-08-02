# Contributing

Contributions are welcome. Keep changes focused, dependency-free where practical, and compatible with Python 3.10 or newer.

## Development workflow

1. Fork the repository and create a branch for the change.
2. Do not commit `.env`, browser cookies, passwords, downloaded course files, or private LMS responses.
3. Add or update tests for behavioral changes.
4. Run:

   ```bash
   python3 -m unittest -v
   python3 -m py_compile ravin_downloader.py
   ```

5. Open a pull request describing the problem, approach, and test coverage.

Tests must use synthetic data. Do not include real course content, account identifiers, session values, or credentials in fixtures or issue reports.
