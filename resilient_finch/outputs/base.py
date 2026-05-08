from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib
    from datetime import datetime

    from ..session import TranscriptEntry


class OutputWriter(ABC):
    @abstractmethod
    def write_header(self, topic: str, start_time: datetime) -> None: ...

    @abstractmethod
    def write_entry(self, entry: TranscriptEntry) -> None: ...

    @abstractmethod
    def write_footer(self, end_time: datetime) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def get_file_path(self) -> pathlib.Path | None:
        return None
