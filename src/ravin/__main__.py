"""Allow ``python -m ravin`` to run the CLI."""

from .cli import main


raise SystemExit(main())
