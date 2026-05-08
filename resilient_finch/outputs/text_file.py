from __future__ import annotations

import pathlib
import re
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .. import config
from .base import OutputWriter

if TYPE_CHECKING:
    from ..session import TranscriptEntry


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _format_duration(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class TextFileWriter(OutputWriter):
    def __init__(
        self,
        topic: str = "",
        sessions_dir: str = config.SESSIONS_DIR,
        filename_format: str = config.SESSION_FILENAME_FORMAT,
    ) -> None:
        self._lock = threading.Lock()
        self._start_time: datetime | None = None
        self._topic = topic.strip()

        sessions_path = pathlib.Path(sessions_dir)
        sessions_path.mkdir(parents=True, exist_ok=True)

        base = datetime.now(tz=UTC).strftime(filename_format)
        if self._topic:
            slug = _slugify(self._topic)
            stem, dot, ext = base.rpartition(".")
            base = f"{stem}_{slug}.{ext}" if dot else f"{stem}_{slug}"

        self._file_path = sessions_path / base
        self._file = self._file_path.open("a", encoding="utf-8")

    def write_header(self, topic: str, start_time: datetime) -> None:
        self._start_time = start_time
        lines: list[str] = ["=" * 60]
        if topic:
            lines.append(f"  Topic:   {topic}")
        lines += [
            f"  Date:    {start_time.strftime('%Y-%m-%d')}",
            f"  Started: {start_time.strftime('%H:%M:%S')} UTC",
            "=" * 60,
            "",
        ]
        with self._lock:
            self._file.write("\n".join(lines) + "\n")
            self._file.flush()

    def write_entry(self, entry: TranscriptEntry) -> None:
        with self._lock:
            self._file.write(entry.format_line())
            self._file.flush()

    def write_footer(self, end_time: datetime) -> None:
        start = self._start_time or end_time
        duration_str = _format_duration(int((end_time - start).total_seconds()))
        lines = [
            "",
            "=" * 60,
            f"  Ended:    {end_time.strftime('%H:%M:%S')} UTC",
            f"  Duration: {duration_str}",
            "=" * 60,
        ]
        with self._lock:
            self._file.write("\n".join(lines) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def get_file_path(self) -> pathlib.Path:
        return self._file_path
