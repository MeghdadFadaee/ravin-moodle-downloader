"""Range-aware local HTTP server for the static learning library."""

import functools
import http.server
import os
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from .models import MoodleError


class _RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static-file handler with byte ranges for efficient local media playback."""

    _byte_range: tuple[int, int] | None = None

    def send_head(self) -> Any:
        self._byte_range = None
        request_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        request_parts = [part for part in request_path.split("/") if part]
        if any(part.startswith(".") for part in request_parts) or request_path.casefold().endswith(".part"):
            self.send_error(404, "File not found")
            return None
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            source = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        try:
            size = os.fstat(source.fileno()).st_size
            range_header = self.headers.get("Range", "")
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:
                    suffix_length = int(match.group(2))
                    start = max(size - suffix_length, 0)
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    source.close()
                    return None
                end = min(end, size - 1)
                self._byte_range = (start, end)
                self.send_response(206)
                self.send_header("Content-Type", self.guess_type(path))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Last-Modified", self.date_time_string(os.fstat(source.fileno()).st_mtime))
                self.end_headers()
                return source
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(os.fstat(source.fileno()).st_mtime))
            self.end_headers()
            return source
        except Exception:
            source.close()
            raise

    def copyfile(self, source: Any, outputfile: Any) -> None:
        if self._byte_range is None:
            shutil.copyfileobj(source, outputfile)
            return
        start, end = self._byte_range
        source.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def _serve_library(site_dir: Path, host: str, port: int, open_browser: bool) -> None:
    site_dir = site_dir.expanduser().resolve()
    if not (site_dir / "index.html").is_file() or not (site_dir / "courses.json").is_file():
        raise MoodleError(f"{site_dir} is not built yet; run `ravin library` first")
    handler = functools.partial(_RangeRequestHandler, directory=str(site_dir))
    try:
        server = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        raise MoodleError(f"could not start the library server on {host}:{port}: {exc}") from exc
    display_host = "localhost" if host in {"127.0.0.1", "0.0.0.0", "::"} else host
    url = f"http://{display_host}:{server.server_port}/"
    print(f"Learning Library: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
