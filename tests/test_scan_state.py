from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravin.scan import scan_offline


class PortableArtifactStateTests(unittest.TestCase):
    def test_timestamp_rounding_does_not_stale_matching_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "public"
            activity = public / "courses" / "44" / "content" / "001--001--100"
            video = activity / "files" / "lesson.mp4"
            transcript = activity / "artifacts" / "transcript.fa.txt"
            video.parent.mkdir(parents=True)
            transcript.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            transcript.write_text("transcript", encoding="utf-8")
            video_stat = video.stat()
            transcript_stat = transcript.stat()
            rounded_video_mtime = video_stat.st_mtime_ns - 1_500_000_000
            rounded_transcript_mtime = transcript_stat.st_mtime_ns - 1_500_000_000
            (activity / "artifacts" / "transcript.meta.json").write_text(
                json.dumps(
                    {
                        "source": "../files/lesson.mp4",
                        "source_size": video_stat.st_size,
                        "source_mtime_ns": rounded_video_mtime,
                    }
                ),
                encoding="utf-8",
            )
            (activity / "artifacts" / "summary.fa.md").write_text("summary", encoding="utf-8")
            (activity / "artifacts" / "summary.meta.json").write_text(
                json.dumps(
                    {
                        "source": "transcript.fa.txt",
                        "source_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                        "source_size": transcript_stat.st_size,
                        "source_mtime_ns": rounded_transcript_mtime,
                    }
                ),
                encoding="utf-8",
            )
            manifest = public / "courses" / "44" / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "course": {
                            "id": 44,
                            "fullname": "Course",
                            "sections": [
                                {
                                    "number": 1,
                                    "items": [
                                        {
                                            "filename": "lesson.mp4",
                                            "activity_type": "resource",
                                            "kind": "video",
                                            "title": "Lesson",
                                            "section_number": 1,
                                            "activity_position": 1,
                                            "activity_id": 100,
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            scan_offline(public, [44])

            item = json.loads(manifest.read_text(encoding="utf-8"))["course"]["sections"][0]["items"][0]
            self.assertEqual(item["state"]["transcript"], "complete")
            self.assertEqual(item["state"]["summary"], "complete")


if __name__ == "__main__":
    unittest.main()
