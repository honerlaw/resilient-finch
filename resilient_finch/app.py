from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
from typing import TYPE_CHECKING

from faster_whisper import WhisperModel

from . import config
from .capture import AudioCapturer, AudioChunk
from .outputs import OutputWriter, TextFileWriter
from .session import Session
from .transcriber import Transcriber

if TYPE_CHECKING:
    import types

logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()


def _shutdown_handler(_signum: int, _frame: types.FrameType | None) -> None:
    sys.stdout.write("\nShutting down... flushing remaining audio (this may take up to 60s)\n")
    _shutdown_event.set()


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


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Real-time dual-stream audio transcription for macOS",
    )
    parser.add_argument(
        "-t",
        "--topic",
        default="",
        metavar="TEXT",
        help='What this session is about, e.g. "daily standup". Included in filename and header.',
    )
    args = parser.parse_args()
    topic: str = args.topic

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    writers = _build_writers(topic)
    session = Session(topic=topic, writers=writers)
    if topic:
        sys.stdout.write(f"Topic:        {topic}\n")
    file_path = session.get_file_path()
    if file_path is not None:
        sys.stdout.write(f"Session file: {file_path}\n")

    sys.stdout.write(
        f"Loading Whisper {config.WHISPER_MODEL} model (first run downloads ~3GB)...\n"
    )
    model = WhisperModel(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
    )
    sys.stdout.write("Model loaded.\n")

    mic_queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=config.AUDIO_QUEUE_MAXSIZE)
    speaker_queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=config.AUDIO_QUEUE_MAXSIZE)

    capturer = AudioCapturer(mic_queue, speaker_queue)
    mic_t = Transcriber("MIC", mic_queue, session, model)
    spk_t = Transcriber("SPEAKER", speaker_queue, session, model)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        capturer.start()
    except RuntimeError as e:
        sys.stdout.write(f"\nError: {e}\n")
        session.close()
        return

    mic_t.start()
    spk_t.start()

    sys.stdout.write("Recording... Press Ctrl+C to stop.\n\n")
    _shutdown_event.wait()

    capturer.stop()
    mic_t.flush_and_stop(timeout=60.0)
    spk_t.flush_and_stop(timeout=60.0)
    session.close()
    saved_path = session.get_file_path()
    if saved_path is not None:
        sys.stdout.write(f"\nSession saved to: {saved_path}\n")
