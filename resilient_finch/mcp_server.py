from __future__ import annotations

import dataclasses
import logging
import pathlib
import queue
import threading

from faster_whisper import WhisperModel
from mcp.server.fastmcp import FastMCP

from . import config
from .capture import AudioCapturer, AudioChunk
from .outputs import OutputWriter, TextFileWriter
from .session import Session
from .transcriber import Transcriber

logger = logging.getLogger(__name__)


def _build_writers(topic: str) -> list[OutputWriter]:
    writers: list[OutputWriter] = []
    for name in config.OUTPUTS:
        if name == "text_file":
            writers.append(TextFileWriter(topic=topic))
        elif name == "google_docs":
            from .outputs.google_docs import GoogleDocsWriter
            writers.append(GoogleDocsWriter())
        else:
            logger.warning("Unknown output type %r — skipping", name)
    return writers


@dataclasses.dataclass
class _ActiveSession:
    session: Session
    capturer: AudioCapturer
    mic_transcriber: Transcriber
    speaker_transcriber: Transcriber


@dataclasses.dataclass
class _ServerState:
    model: WhisperModel | None = None
    active: _ActiveSession | None = None
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


_state = _ServerState()
mcp = FastMCP("resilient-finch", description="Real-time audio transcription for macOS")


def _ensure_model() -> WhisperModel:
    with _state.lock:
        if _state.model is None:
            _state.model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
            )
        return _state.model


@mcp.tool()
def start_session(topic: str = "") -> str:
    """Start capturing microphone and system audio and begin transcribing.

    Optionally provide a topic label such as 'daily standup' or '1:1 with Alex'.
    """
    model = _ensure_model()

    with _state.lock:
        if _state.active is not None:
            return "A session is already running. Call stop_session first."

        session = Session(topic=topic, writers=_build_writers(topic))
        mic_queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=config.AUDIO_QUEUE_MAXSIZE)
        spk_queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=config.AUDIO_QUEUE_MAXSIZE)
        capturer = AudioCapturer(mic_queue, spk_queue)
        mic_t = Transcriber("MIC", mic_queue, session, model)
        spk_t = Transcriber("SPEAKER", spk_queue, session, model)

        try:
            capturer.start()
        except RuntimeError as e:
            session.close()
            return f"Failed to start audio capture: {e}"

        mic_t.start()
        spk_t.start()

        _state.active = _ActiveSession(
            session=session,
            capturer=capturer,
            mic_transcriber=mic_t,
            speaker_transcriber=spk_t,
        )

        topic_suffix = f" (topic: {topic})" if topic else ""
        file_path = session.get_file_path()
        location = str(file_path) if file_path is not None else ", ".join(config.OUTPUTS)
        return f"Session started{topic_suffix}. Saving to: {location}"


@mcp.tool()
def stop_session() -> str:
    """Stop the current transcription session and flush all remaining buffered audio.

    May take up to 60 seconds to complete.
    """
    with _state.lock:
        if _state.active is None:
            return "No session is currently running."
        active = _state.active
        _state.active = None

    active.capturer.stop()
    active.mic_transcriber.flush_and_stop(timeout=60.0)
    active.speaker_transcriber.flush_and_stop(timeout=60.0)
    active.session.close()

    file_path = active.session.get_file_path()
    location = str(file_path) if file_path is not None else ", ".join(config.OUTPUTS)
    return f"Session stopped. Saved to: {location}"


@mcp.tool()
def get_transcript() -> str:
    """Get the full transcript from the currently running session.

    Returns entries sorted chronologically with [MIC] and [SPEAKER] labels.
    """
    with _state.lock:
        if _state.active is None:
            return "No session is currently running."
        entries = _state.active.session.get_transcript()

    if not entries:
        return "No transcript entries yet."

    return "".join(e.format_line() for e in entries)


@mcp.tool()
def list_sessions() -> str:
    """List all saved transcription session files, newest first.

    Marks the currently active session if one is running.
    """
    sessions_path = pathlib.Path(config.SESSIONS_DIR)
    if not sessions_path.exists():
        return "No sessions directory found."

    files = sorted(sessions_path.glob("session_*.txt"), reverse=True)
    if not files:
        return "No sessions found."

    with _state.lock:
        active_path = (
            _state.active.session.get_file_path() if _state.active is not None else None
        )

    lines: list[str] = []
    for f in files:
        size_kb = f.stat().st_size / 1024
        marker = "  ← active" if f == active_path else ""
        lines.append(f"{f.name}  ({size_kb:.1f} KB){marker}")

    return "\n".join(lines)


@mcp.tool()
def read_session(filename: str) -> str:
    """Read the full content of a saved session file.

    Use list_sessions first to see available filenames.
    """
    sessions_path = pathlib.Path(config.SESSIONS_DIR).resolve()
    candidate = (sessions_path / filename).resolve()

    if not candidate.is_relative_to(sessions_path):
        return "Invalid filename: path traversal not allowed."
    if not candidate.exists():
        return f"Session file not found: {filename}"

    try:
        return candidate.read_text(encoding="utf-8")
    except OSError as e:
        return f"Failed to read session file: {e}"


def main() -> None:
    _ensure_model()
    mcp.run()


if __name__ == "__main__":
    main()
