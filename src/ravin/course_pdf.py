"""Offline course summary PDFs, rendered with Unicode shaping in Chromium."""
from __future__ import annotations

import base64
import hashlib
from html import escape
from pathlib import Path
import tempfile
from urllib.parse import unquote

from .models import MoodleError
from .paths import _read_json_object, _relative_browser_path
from .scan import scan_offline


def summary_sources(public: Path, course: dict) -> list[tuple[str, str, Path]]:
    """Read summaries in manifest order, once per activity bundle."""
    sources = []
    seen = set()
    root = (public / "courses" / str(course["id"])).resolve()
    for section in course.get("sections", []):
        for item in section.get("items", []):
            url = item.get("artifacts", {}).get("summary", {}).get("url")
            if not url:
                continue
            path = (public / unquote(url)).resolve()
            if not path.is_relative_to(root):
                raise MoodleError(f"summary is outside its course directory: {url}")
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if path.read_text(encoding="utf-8").strip():
                sources.append((section.get("title") or section.get("name") or "", item.get("title") or "", path))
    return sources


def source_digest(course: dict, sources: list[tuple[str, str, Path]]) -> str:
    digest = hashlib.sha256(str(course.get("fullname", "")).encode())
    for chapter, title, path in sources:
        digest.update((chapter + "\0" + title + "\0").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def summary_pdf_artifact(public: Path, course: dict) -> dict | None:
    path = public / "courses" / str(course["id"]) / "artifacts" / "summaries.fa.pdf"
    if not path.is_file():
        return None
    metadata = _read_json_object(path.with_suffix(".meta.json"))
    sources = summary_sources(public, course)
    return {"url": _relative_browser_path(path, public), "format": "pdf", "language": "fa",
            "summary_count": metadata.get("summary_count", 0),
            "state": "complete" if metadata.get("source_sha256") == source_digest(course, sources) else "stale"}


def render_html(course: dict, sources: list[tuple[str, str, Path]]) -> str:
    from markdown_it import MarkdownIt
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    # No external resources or raw HTML from summaries are loaded by the renderer.
    markdown.disable("image")
    faces = []
    for weight, name in ((400, "Regular"), (700, "Bold")):
        data = base64.b64encode((Path(__file__).parent / "fonts" / f"Vazirmatn-{name}.ttf").read_bytes()).decode()
        faces.append(f"@font-face{{font-family:Vazirmatn;font-weight:{weight};src:url(data:font/ttf;base64,{data})}}")
    body = [f'<h1 dir="auto">{escape(course.get("fullname") or str(course["id"]))}</h1>']
    previous = None
    for chapter, title, path in sources:
        if chapter != previous:
            body.append(f'<h2 dir="auto">{escape(chapter)}</h2>')
            previous = chapter
        content = markdown.render(path.read_text(encoding="utf-8"))
        body.append(f'<section><h3 dir="auto">{escape(title)}</h3>{content}</section>')
    return '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><style>' + ''.join(faces) + '''
@page { size:A4; margin:18mm 16mm 20mm; }
body {font-family:Vazirmatn; font-size:11pt; line-height:1.9; color:#182334;}
h1 {font-size:24pt;} h2 {font-size:18pt; border-bottom:1px solid #ccd5df;}
h3 {font-size:14pt;} h1,h2,h3 {break-after:avoid;}
p,li,td,th {unicode-bidi:plaintext; overflow-wrap:anywhere;}
pre {direction:ltr; text-align:left; white-space:pre-wrap; unicode-bidi:isolate;}
code {font-family:Vazirmatn; unicode-bidi:isolate; direction:ltr;}
table {width:100%; border-collapse:collapse; font-size:10pt;}
th,td {border:1px solid #ccd5df; padding:5px;}
tr,blockquote {break-inside:avoid;} a {color:#245b8c;}
</style><body>''' + ''.join(body) + '</body></html>'


def create_course_pdf(public: Path, course_id: int) -> dict:
    """Atomically replace the course PDF and refresh Library manifests."""
    from .scan import _atomic_json
    public = public.expanduser().resolve()
    scan_offline(public, [course_id])
    course = _read_json_object(public / "courses" / str(course_id) / "manifest.json")["course"]
    sources = summary_sources(public, course)
    if not sources:
        raise MoodleError(f"course {course_id} has no non-empty summaries; run `ravin summarize {course_id}` first")
    try:
        from playwright.sync_api import sync_playwright, Error
        html = render_html(course, sources)
    except ImportError as exc:
        raise MoodleError('PDF support requires `pip install "ravin-moodle-downloader[pdf]"` and `python -m playwright install chromium`') from exc
    target = public / "courses" / str(course_id) / "artifacts" / "summaries.fa.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = source_digest(course, sources)
    try:
        with tempfile.TemporaryDirectory(prefix=".pdf-", dir=target.parent) as temporary:
            output = Path(temporary) / "course.pdf"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    page.route("**/*", lambda route: route.abort())
                    page.set_content(html, wait_until="load")
                    page.evaluate("document.fonts.ready")
                    page.pdf(path=str(output), format="A4", print_background=True,
                             prefer_css_page_size=True, display_header_footer=True,
                             header_template="<span></span>",
                             footer_template='<div style="width:100%;text-align:center;font-size:9px"><span class="pageNumber"></span> / <span class="totalPages"></span></div>')
                finally:
                    browser.close()
            output.replace(target)
    except Error as exc:
        raise MoodleError(f"PDF rendering failed; install Chromium with `python -m playwright install chromium`: {exc}") from exc
    _atomic_json(target.with_suffix(".meta.json"), {"source_sha256": digest, "summary_count": len(sources)})
    scan_offline(public, [course_id])
    return {"course_id": course_id, "path": str(target), "summary_count": len(sources)}
