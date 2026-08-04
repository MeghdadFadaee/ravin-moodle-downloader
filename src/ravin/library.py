"""Compatibility facade for the static library subsystems."""

from .catalog import (
    _build_library_catalog,
    _format_size,
    _reuse_library_catalog,
    _write_library_site,
)
from .migration import _migrate_legacy_downloads
from .server import _serve_library

__all__ = [
    "_build_library_catalog",
    "_format_size",
    "_migrate_legacy_downloads",
    "_reuse_library_catalog",
    "_serve_library",
    "_write_library_site",
]
