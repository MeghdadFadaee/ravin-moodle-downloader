"""Resilient manifest-driven Whisper transcription."""

from __future__ import annotations

import gc
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import MoodleError
from .paths import _clean_name, _read_json_object
from .scan import scan_offline


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_text(path: Path, content: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int | None = None) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=mode)


def _append_json_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _compact_error(error: BaseException, limit: int = 4000) -> str:
    message = str(error).strip() or type(error).__name__
    return message if len(message) <= limit else message[:limit] + "\n... error truncated ..."


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.seek(0)
            owner = lock.read().strip() or "unknown"
            raise MoodleError(f"another transcription run is active (PID {owner})") from exc
        except (ImportError, OSError):
            pass
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        yield


def _start_caffeinate(enabled: bool) -> subprocess.Popen[bytes] | None:
    executable = shutil.which("caffeinate")
    if not enabled or sys.platform != "darwin" or not executable:
        return None
    return subprocess.Popen(
        [executable, "-i", "-w", str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _select_device(torch: Any, requested: str) -> str:
    device = requested.casefold()
    if device == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise MoodleError("CUDA was requested, but no CUDA device is available")
    if device == "mps" and not torch.backends.mps.is_available():
        raise MoodleError("MPS was requested, but Apple Metal acceleration is unavailable")
    if device not in {"cpu", "cuda", "mps"}:
        raise MoodleError("--device must be one of: auto, cpu, cuda, mps")
    return device


def _load_model(model_name: str, requested_device: str) -> tuple[Any, Any, str]:
    try:
        import torch
        import whisper
    except ImportError as exc:
        raise MoodleError(
            "Whisper support is not installed; run `python3 -m pip install '.[transcribe]'`"
        ) from exc
    device = _select_device(torch, requested_device)
    print(f"Loading Whisper model {model_name!r} on {device}...", file=sys.stderr)
    try:
        model = whisper.load_model(model_name, device=device)
    except Exception as exc:
        raise MoodleError(f"could not load Whisper model {model_name!r}: {_compact_error(exc)}") from exc
    return model, torch, device


def _validate_media(source: Path) -> None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except FileNotFoundError as exc:
        raise MoodleError("FFmpeg is required; install it with `brew install ffmpeg`") from exc
    if result.returncode != 0:
        reason = result.stderr.strip().splitlines()[-1:] or ["invalid media file"]
        raise ValueError(reason[0])
    if not result.stdout.strip():
        raise ValueError("media file contains no audio stream")


@dataclass(frozen=True)
class TranscriptionJob:
    course_id: int
    activity_id: int | None
    key: str
    title: str
    source: Path
    transcript: Path
    metadata: Path
    error_marker: Path

    def label(self) -> str:
        return f"course {self.course_id} / {self.key} / {self.source.name}"


@dataclass(frozen=True)
class TranscriptionOptions:
    public: Path
    course_ids: tuple[int, ...] = ()
    model: str = "large"
    device: str = "auto"
    language: str | None = "fa"
    retries: int = 2
    overwrite: bool = False
    dry_run: bool = False
    keep_awake: bool = sys.platform == "darwin"
    validate_media: bool = True


@dataclass
class TranscriptionResult:
    status: str
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    unavailable: int = 0
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CourseTranscriber:
    def __init__(
        self,
        options: TranscriptionOptions,
        *,
        model_loader: Callable[[str, str], tuple[Any, Any, str]] = _load_model,
        media_validator: Callable[[Path], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.options = options
        self.public = options.public.expanduser().resolve()
        self.model_loader = model_loader
        self.media_validator = media_validator or _validate_media
        self.requires_ffprobe = media_validator is None
        self.sleeper = sleeper
        self.runtime_root = self.public.parent / ".ravin" / "transcribe"
        self.state_path = self.runtime_root / "state.json"
        self.summary_path = self.runtime_root / "summary.json"
        self.errors_path = self.runtime_root / "errors.jsonl"
        self.started_at = _utc_now()
        self.result = TranscriptionResult(status="starting")
        self.current_index = 0
        self.current_job: TranscriptionJob | None = None
        self._torch: Any | None = None

    def _write_state(self) -> None:
        _atomic_json(
            self.state_path,
            {
                **self.result.to_dict(),
                "pid": os.getpid(),
                "started_at": self.started_at,
                "updated_at": _utc_now(),
                "model": self.options.model,
                "device": self.options.device,
                "language": self.options.language,
                "course_ids": list(self.options.course_ids),
                "current_index": self.current_index,
                "current": self.current_job.label() if self.current_job else None,
            },
        )

    def _finish(self, status: str, exit_code: int) -> TranscriptionResult:
        self.result.status = status
        self.result.exit_code = exit_code
        self.current_job = None
        self._write_state()
        _atomic_json(
            self.summary_path,
            {
                **self.result.to_dict(),
                "started_at": self.started_at,
                "finished_at": _utc_now(),
                "model": self.options.model,
                "device": self.options.device,
                "language": self.options.language,
                "course_ids": list(self.options.course_ids),
            },
        )
        return self.result

    def _discover_jobs(self) -> list[TranscriptionJob]:
        courses_root = self.public / "courses"
        selected = set(self.options.course_ids)
        manifests = sorted(courses_root.glob("*/manifest.json"))
        jobs: list[TranscriptionJob] = []
        found: set[int] = set()
        unavailable = 0
        for manifest_path in manifests:
            manifest = _read_json_object(manifest_path)
            course = manifest.get("course")
            if not isinstance(course, dict):
                continue
            course_id = int(course.get("id") or 0)
            if selected and course_id not in selected:
                continue
            found.add(course_id)
            for section in course.get("sections", []):
                for item in section.get("items", []):
                    if item.get("kind") not in {"video", "audio"}:
                        continue
                    key = str(item.get("key") or item.get("bundle_path", "").removeprefix("content/"))
                    activity = courses_root / str(course_id) / "content" / key
                    expected = activity / "files" / _clean_name(str(item.get("filename") or ""), "file")
                    candidates = sorted(
                        path for path in (activity / "files").glob("*")
                        if path.is_file() and not path.name.endswith(".part")
                    )
                    source = expected if expected.is_file() else (candidates[0] if len(candidates) == 1 else None)
                    if source is None:
                        unavailable += 1
                        continue
                    artifacts = activity / "artifacts"
                    jobs.append(
                        TranscriptionJob(
                            course_id=course_id,
                            activity_id=item.get("activity_id"),
                            key=key,
                            title=str(item.get("title") or source.name),
                            source=source,
                            transcript=artifacts / "transcript.fa.txt",
                            metadata=artifacts / "transcript.meta.json",
                            error_marker=artifacts / ".transcript.error.json",
                        )
                    )
        missing = selected - found
        if missing:
            raise MoodleError(f"course manifest(s) not found: {', '.join(str(value) for value in sorted(missing))}")
        if not found:
            raise MoodleError(f"no course manifests found in {courses_root}; run `ravin scan` first")
        self.result.unavailable = unavailable
        return jobs

    def _expected_metadata(self, job: TranscriptionJob) -> dict[str, Any]:
        source_stat = job.source.stat()
        return {
            "schema_version": 1,
            "source": f"../files/{job.source.name}",
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "model": self.options.model,
            "language": self.options.language,
            "transcript": "transcript.fa.txt",
        }

    def _has_matching_transcript(self, job: TranscriptionJob) -> bool:
        if not job.transcript.is_file() or not job.metadata.is_file():
            return False
        metadata = _read_json_object(job.metadata)
        try:
            expected = self._expected_metadata(job)
        except OSError:
            return False
        return all(metadata.get(key) == value for key, value in expected.items())

    def _clear_accelerator_cache(self) -> None:
        gc.collect()
        if self._torch is None:
            return
        try:
            if self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()
            if self._torch.backends.mps.is_available():
                self._torch.mps.empty_cache()
        except Exception:
            pass

    def _with_retries(self, operation: Callable[[], Any], job: TranscriptionJob, stage: str) -> Any:
        attempts = self.options.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._clear_accelerator_cache()
                if attempt == attempts:
                    raise
                delay = min(2 ** (attempt - 1), 30)
                print(
                    f"  {stage} attempt {attempt}/{attempts} failed: {_compact_error(exc)}; "
                    f"retrying in {delay}s",
                    file=sys.stderr,
                )
                self.sleeper(delay)
        raise AssertionError("unreachable")

    def _record_failure(self, job: TranscriptionJob, error: BaseException) -> None:
        message = _compact_error(error)
        private_record = {
            "failed_at": _utc_now(),
            "course_id": job.course_id,
            "activity_id": job.activity_id,
            "activity_key": job.key,
            "source": job.source.relative_to(self.public).as_posix(),
            "error_type": type(error).__name__,
            "error": message,
            "model": self.options.model,
            "language": self.options.language,
            "attempts": self.options.retries + 1,
        }
        _append_json_line(self.errors_path, private_record)
        public_message = message.replace(str(job.source), job.source.name)
        _atomic_json(
            job.error_marker,
            {
                "schema_version": 1,
                "failed_at": private_record["failed_at"],
                "error_type": private_record["error_type"],
                "error": public_message,
                "model": self.options.model,
                "language": self.options.language,
                "attempts": self.options.retries + 1,
            },
        )
        self.result.failed += 1

    def _write_transcript(self, job: TranscriptionJob, text: str, device: str) -> None:
        metadata = self._expected_metadata(job)
        metadata.update({"device": device, "generated_at": _utc_now()})
        _atomic_text(job.transcript, text.strip() + "\n", mode=0o644)
        _atomic_json(job.metadata, metadata, mode=0o644)
        job.error_marker.unlink(missing_ok=True)

    def run(self) -> TranscriptionResult:
        if self.options.retries < 0:
            raise MoodleError("--retries cannot be negative")
        if not self.options.dry_run:
            scan_offline(self.public, self.options.course_ids)
        jobs = self._discover_jobs()
        self.result.total = len(jobs)
        matching = {job for job in jobs if not self.options.overwrite and self._has_matching_transcript(job)}
        if not self.options.dry_run:
            for job in matching:
                job.error_marker.unlink(missing_ok=True)
        self.result.skipped = len(matching)
        pending = [job for job in jobs if job not in matching]

        if self.options.dry_run:
            for job in pending:
                # Keep stdout machine-readable when the CLI is used with --json.
                print(f"would transcribe: {job.label()}", file=sys.stderr)
            self.result.status = "dry_run"
            return self.result
        if not pending:
            return self._finish("completed", 0)
        if self.options.validate_media and self.requires_ffprobe and not shutil.which("ffprobe"):
            raise MoodleError("FFmpeg is required; install it with `brew install ffmpeg`")

        self.result.status = "running"
        self._write_state()
        try:
            model, self._torch, device = self.model_loader(self.options.model, self.options.device)
        except Exception:
            self._finish("failed_to_start", 2)
            raise
        for index, job in enumerate(jobs, start=1):
            self.current_index = index
            if job in matching:
                continue
            self.current_job = job
            self._write_state()
            print(f"[{index}/{len(jobs)}] {job.title} ({job.source.name})", file=sys.stderr)
            try:
                if self.options.validate_media:
                    self._with_retries(lambda: self.media_validator(job.source), job, "validation")
                result = self._with_retries(
                    lambda: model.transcribe(str(job.source), language=self.options.language, verbose=False),
                    job,
                    "transcription",
                )
                text = str(result.get("text") or "").strip()
                self._write_transcript(job, text, str(getattr(model, "device", device)))
                self.result.succeeded += 1
                print("  completed", file=sys.stderr)
            except KeyboardInterrupt:
                return self._finish("interrupted", 130)
            except Exception as exc:
                self._record_failure(job, exc)
                print(f"  failed permanently; continuing: {_compact_error(exc)}", file=sys.stderr)
            finally:
                scan_offline(self.public, [job.course_id])
                self.current_job = None
                self._write_state()

        status = "completed_with_errors" if self.result.failed else "completed"
        return self._finish(status, 1 if self.result.failed else 0)


def _raise_keyboard_interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def transcribe_courses(options: TranscriptionOptions) -> TranscriptionResult:
    transcriber = CourseTranscriber(options)
    if options.dry_run:
        return transcriber.run()
    caffeinate = _start_caffeinate(options.keep_awake and not options.dry_run)
    previous_sigterm: Any = None
    signal_installed = False
    if hasattr(signal, "SIGTERM"):
        try:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
            signal_installed = True
        except ValueError:
            pass
    try:
        with _exclusive_lock(transcriber.runtime_root / "run.lock"):
            return transcriber.run()
    finally:
        if signal_installed and previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if caffeinate is not None:
            caffeinate.terminate()


def format_transcription_result(result: TranscriptionResult) -> str:
    return (
        f"Transcription {result.status}: {result.succeeded} succeeded, "
        f"{result.skipped} skipped, {result.failed} failed, "
        f"{result.unavailable} unavailable ({result.total} local media files)."
    )
