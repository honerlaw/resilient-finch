from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .outputs.base import OutputWriter


@dataclass(order=True)
class TranscriptEntry:
    timestamp: datetime
    source: str = field(compare=False)
    text: str = field(compare=False)

    def format_line(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S")
        return f"[{ts}] [{self.source}] {self.text.strip()}\n"


class Session:
    def __init__(self, topic: str = "", writers: list[OutputWriter] | None = None) -> None:
        self._start_time = datetime.now(tz=UTC)
        self._topic = topic.strip()
        self._writers: list[OutputWriter] = writers if writers is not None else []
        self._entries: list[TranscriptEntry] = []
        self._lock = threading.Lock()
        self._closed = False

        for w in self._writers:
            w.write_header(self._topic, self._start_time)

    def add_entry(self, timestamp: datetime, source: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        entry = TranscriptEntry(timestamp=timestamp, source=source, text=text)
        with self._lock:
            self._entries.append(entry)
            for w in self._writers:
                w.write_entry(entry)

    def get_transcript(self) -> list[TranscriptEntry]:
        with self._lock:
            return sorted(self._entries)

    def get_file_path(self) -> pathlib.Path | None:
        for w in self._writers:
            p = w.get_file_path()
            if p is not None:
                return p
        return None

    def get_topic(self) -> str:
        return self._topic

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            end_time = datetime.now(tz=UTC)
            for w in self._writers:
                w.write_footer(end_time)
                w.close()
            self._closed = True
